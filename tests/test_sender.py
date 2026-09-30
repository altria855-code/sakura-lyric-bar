import unittest

import _path  # noqa: F401
import sender


class FakeLogger:
    """四个级别各是一个方法 —— 不能写成 `debug = info = warning = error = _record`:
    那样通过实例调用时 `self` 会被当成 `level`,而 `fields=` 是关键字参数,
    于是 `message` 缺失、直接抛 TypeError(预验证实测)。
    记录 (level, message, fields) 三元组,好让用例能断言级别与字段。"""

    def __init__(self):
        self.records = []

    def _record(self, level, message, fields=None):
        self.records.append((level, message, dict(fields or {})))

    def debug(self, message, fields=None):
        self._record("debug", message, fields)

    def info(self, message, fields=None):
        self._record("info", message, fields)

    def warning(self, message, fields=None):
        self._record("warning", message, fields)

    def error(self, message, fields=None):
        self._record("error", message, fields)


class FakeConversation:
    """**必须**与宿主的真实返回同形:`{"status": ...}`,不是 `{"state": ...}`。

    宿主的 `poll`(core/app/core_host/conversation_host.py:poll)返回
    `{"status": "running"}` / `{"status": "completed", "result": {...}}`。
    这个假件以前返回 `state`,于是"实现读错字段名"在测试里全绿、真机上每次发送都报失败。
    """

    def __init__(self, states, begin_error=None):
        self.states = list(states)
        self.begin_error = begin_error
        self.cancelled = []
        self.begun = []

    def begin(self, character_id, text, artifact):
        self.begun.append((character_id, text))
        if self.begin_error:
            raise RuntimeError(self.begin_error)
        return {"jobId": "job-1"}

    def poll(self, job_id):
        if self.states:
            return {"jobId": job_id, "status": self.states.pop(0)}
        return {"jobId": job_id, "status": "completed"}

    def cancel(self, job_id):
        self.cancelled.append(job_id)


class HostError(RuntimeError):
    """宿主异常的形状:带 `code` 属性(conversation_host.ConversationHostError 就是这么实现的)。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class HostLikeConversation:
    """按**宿主真实语义**收尾的假件:job 在"被轮询到完成"的那一次就被删掉。

    `conversation_host.poll()` 是 `if not job.done: return running` / `_jobs.pop(job_id)`
    —— 也就是说**完成之后不存在第二次 poll**:再来一次只会抛
    `CHAT_JOB_NOT_FOUND`。这条语义正是真机事故的成因,所以必须有一个假件照着实现,
    否则「轮询循环有没有在读完成态」永远测不出来(见 test_completed_job_is_never_polled_twice)。
    """

    def __init__(self, running_polls=0, error_code=None):
        self.running_polls = running_polls
        self.error_code = error_code
        self.polls = 0
        self.alive = True

    def begin(self, character_id, text, artifact):
        return {"jobId": "job-1"}

    def poll(self, job_id):
        if not self.alive:
            raise HostError("CHAT_JOB_NOT_FOUND")
        self.polls += 1
        if self.running_polls > 0:
            self.running_polls -= 1
            return {"status": "running"}
        self.alive = False  # 宿主:pop 掉这个 job
        if self.error_code:
            raise HostError(self.error_code)  # 失败也是在这里抛(conversation_host.py:poll)
        return {"status": "completed", "result": {"character_id": "tian"}}

    def cancel(self, job_id):
        if not self.alive:
            raise HostError("CHAT_JOB_NOT_FOUND")
        self.alive = False
        return {"accepted": True}


class SenderTests(unittest.TestCase):
    def _make(self, states=("completed",), begin_error=None):
        conversation = FakeConversation(states, begin_error)
        logger = FakeLogger()
        instance = sender.Sender(conversation, logger, sleep=lambda _s: None)
        return instance, conversation, logger

    def test_successful_send(self):
        instance, conversation, _ = self._make()
        result = instance.send("tian", "你好")
        self.assertTrue(result["ok"])
        self.assertEqual(conversation.begun, [("tian", "你好")])

    def test_begin_failure_is_reported_with_code(self):
        instance, _conversation, logger = self._make(begin_error="CHAT_FAILED")
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SEND_FAILED")

    def test_host_error_code_is_passed_through(self):
        # 宿主抛出的异常带 code 属性时必须透传,否则调用方无法区分"通道忙"和"真失败"
        class BusyError(RuntimeError):
            code = "CHAT_EXECUTION_LIMIT_EXCEEDED"

        instance, _conversation, logger = self._make()
        instance._conversation.begin = lambda *a, **k: (_ for _ in ()).throw(BusyError())
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CHAT_EXECUTION_LIMIT_EXCEEDED")
        self.assertTrue(logger.records)  # 失败要留下日志
        self.assertTrue(logger.records)

    def test_poll_until_completed(self):
        instance, conversation, _ = self._make(states=("queued", "running", "completed"))
        result = instance.send("tian", "你好")
        self.assertTrue(result["ok"])
        self.assertFalse(conversation.cancelled)

    def test_failed_state_reports_failure(self):
        instance, _conversation, _ = self._make(states=("failed",))
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SEND_FAILED")

    def test_timeout_cancels_job(self):
        conversation = FakeConversation([])
        conversation.poll = lambda job_id: {"status": "running"}
        clock = {"now": 0.0}

        def sleep(seconds):
            clock["now"] += seconds

        logger = FakeLogger()
        instance = sender.Sender(conversation, logger, timeout=2.0, sleep=sleep, clock=lambda: clock["now"])
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SEND_TIMEOUT")
        self.assertEqual(conversation.cancelled, ["job-1"])

    def test_poll_failure_cancels_the_job(self):
        # poll 抛异常时必须取消宿主那边的任务:否则 finally 清空 _active_job 之后
        # 就再也没机会取消它,而消息其实可能已投递(角色正在回复),用户却看到失败。
        conversation = FakeConversation([])
        calls: list[str] = []
        conversation.cancel = lambda job_id: calls.append(job_id)
        conversation.poll = lambda job_id: (_ for _ in ()).throw(RuntimeError("boom"))
        instance = sender.Sender(
            conversation, FakeLogger(), sleep=lambda _s: None
        )
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["job-1"])

    def test_completed_job_is_never_polled_twice(self):
        """宿主语义级用例:完成之后再 poll 必抛 CHAT_JOB_NOT_FOUND。

        真机事故就是这条:实现读错字段名(`state`)⇒ 不认得 completed ⇒ 继续轮询 ⇒
        第二次 poll 撞上 JOB_NOT_FOUND ⇒ 报"发送失败",而消息其实已被宿主受理。
        用 `HostLikeConversation`(完成即删 job)复现同一个坑:读对字段名才会 ok。
        """
        conversation = HostLikeConversation(running_polls=1)
        instance = sender.Sender(
            conversation, FakeLogger(), sleep=lambda _s: None
        )
        result = instance.send("tian", "你好")
        self.assertTrue(result["ok"], f"消息已被受理,不该报失败:{result}")
        self.assertEqual(conversation.polls, 2, "完成那一次之后不允许再 poll(宿主已经删了 job)")

    def test_host_failure_surfaces_as_failure_not_success(self):
        # 反向用例:宿主 poll 抛错误码(失败也走这条路径,conversation_host.py:poll)时
        # 不能因为"轮询结束了"就当成成功。
        conversation = HostLikeConversation(error_code="CHAT_FAILED")
        instance = sender.Sender(
            conversation, FakeLogger(), sleep=lambda _s: None
        )
        result = instance.send("tian", "你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CHAT_FAILED")

    def test_cancelling_a_finished_job_is_not_reported_as_a_failure(self):
        """任务已结束时宿主的 cancel() 会抛 CHAT_JOB_NOT_FOUND。

        那本来就是"取消一个已经跑完的任务",不是故障 —— 不许记成 SEND_CANCEL_FAILED
        (真机日志里正是这条噪音把根因盖住了),也不许让异常逃出去。
        """
        conversation = HostLikeConversation()
        logger = FakeLogger()
        instance = sender.Sender(conversation, logger, sleep=lambda _s: None)
        instance.send("tian", "你好")  # 任务跑完,宿主已把 job 删掉
        instance._active_job = "job-1"  # 模拟"取消晚了一步":send 的 finally 已清空
        instance.cancel_current()  # 不许抛
        self.assertEqual(instance._active_job, "")
        codes = [fields.get("reason_code") for _level, _msg, fields in logger.records]
        self.assertIn("SEND_JOB_ALREADY_DONE", codes)
        self.assertNotIn("SEND_CANCEL_FAILED", codes, "已结束的任务不该记成取消失败")

    def test_a_real_cancel_failure_is_still_reported(self):
        # 反向用例:别把"任务已结束"的宽容扩成"什么都吞"—— 真故障仍要留下 SEND_CANCEL_FAILED
        conversation = HostLikeConversation()
        conversation.cancel = lambda job_id: (_ for _ in ()).throw(RuntimeError("boom"))
        logger = FakeLogger()
        instance = sender.Sender(conversation, logger, sleep=lambda _s: None)
        instance._active_job = "job-1"
        instance.cancel_current()
        codes = [fields.get("reason_code") for _level, _msg, fields in logger.records]
        self.assertIn("SEND_CANCEL_FAILED", codes)

    def test_empty_text_is_rejected(self):
        instance, conversation, _ = self._make()
        result = instance.send("tian", "   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SEND_EMPTY")
        self.assertEqual(conversation.begun, [])

    def test_logs_do_not_contain_text(self):
        # 必须让断言跑在**会写日志**的路径上:成功路径零日志,断言循环体执行 0 次,
        # 那样"把用户文本拼进 error 日志"的错误实现照样全绿(上游评审实测)。
        instance, _conversation, logger = self._make(begin_error="CHAT_FAILED")
        instance.send("tian", "秘密内容")
        self.assertTrue(logger.records, "失败路径应当留下日志,否则本用例没有判别力")
        for _level, message, fields in logger.records:
            self.assertNotIn("秘密内容", message)
            for value in fields.values():
                self.assertNotIn("秘密内容", str(value))


if __name__ == "__main__":
    unittest.main()
