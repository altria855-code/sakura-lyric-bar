"""通过 sakura.host.mobile 发送用户文本。"""

from __future__ import annotations

import time
from typing import Any, Callable

POLL_INTERVAL_SECONDS = 0.5
SEND_TIMEOUT_SECONDS = 120.0


class Sender:
    def __init__(
        self,
        mobile: Any,
        plugin_id: str,
        log: Any,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = SEND_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._mobile = mobile
        self._plugin_id = plugin_id
        self._log = log
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._active_job = ""

    def cancel_current(self) -> None:
        """取消宿主那边仍在跑的发送任务。

        「任务已经结束」不是失败:宿主在**轮询到完成的那一刻**就把 job 从表里删掉了
        (`mobile_host.py` 的 `poll`:先 `_jobs.pop` 再返回),此后 `cancel()` 会抛
        `MOBILE_CHAT_JOB_NOT_FOUND`。要取消的本来就是那个已经跑完的任务 —— 按 debug 记,
        别记成 SEND_CANCEL_FAILED 把真故障淹掉(真机日志里就是这条噪音盖住了根因)。
        """
        job_id = self._active_job
        if not job_id:
            return
        try:
            self._mobile.cancel(self._plugin_id, job_id)
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "MOBILE_CHAT_JOB_NOT_FOUND":
                self._log.debug(
                    "发送任务已结束,无需取消",
                    fields={"operation": "cancel_send", "reason_code": "SEND_JOB_ALREADY_DONE",
                            "error_type": type(error).__name__},
                )
            else:
                self._log.debug(
                    "取消发送任务失败",
                    fields={"operation": "cancel_send", "reason_code": "SEND_CANCEL_FAILED",
                            "error_type": type(error).__name__},
                )
        finally:
            self._active_job = ""

    def send(self, character_id: str, text: str) -> dict[str, Any]:
        value = text.strip() if isinstance(text, str) else ""
        if not value:
            return {"ok": False, "code": "SEND_EMPTY"}

        try:
            started = self._mobile.begin(self._plugin_id, character_id, value, None)
        except Exception as error:
            # 宿主的错误码要**透传**(例如 MOBILE_CHAT_UNAVAILABLE = 聊天通道当前忙),
            # 调用方据此区分「还在回复中」与真正的失败 —— 一律压成通用码会丢掉这个信息。
            code = getattr(error, "code", None)
            code = code if isinstance(code, str) and code else "MOBILE_SEND_FAILED"
            self._log.error(
                "发送消息失败",
                fields={"operation": "send_message", "reason_code": code,
                        "error_type": type(error).__name__},
            )
            return {"ok": False, "code": code}

        job_id = ""
        if isinstance(started, dict):
            job_id = str(started.get("jobId") or "")
        if not job_id:
            self._log.error(
                "发送未返回任务号",
                fields={"operation": "send_message", "reason_code": "MOBILE_SEND_FAILED"},
            )
            return {"ok": False, "code": "MOBILE_SEND_FAILED"}

        self._active_job = job_id
        deadline = self._clock() + self._timeout
        try:
            while True:
                try:
                    state = self._mobile.poll(self._plugin_id, job_id)
                except Exception as error:
                    # 宿主在 job 完成/失败的那一次 poll 里就把它从 `_jobs` 里删掉了
                    # (mobile_host.py 的 poll:先 pop 再返回),所以**完成之后绝不能再轮询**
                    # 第二次 —— 那会抛 MOBILE_CHAT_JOB_NOT_FOUND,被这里当成"发送失败",
                    # 而消息其实早已投递、角色正在回复(真机实测:每次发送都报失败)。
                    # 先取消宿主那边仍在跑的任务 —— 否则 finally 清空 _active_job 之后
                    # 就再也没有机会取消它,消息其实已投递、角色正在回复,而用户看到
                    # "发送失败"并可能重发。
                    self.cancel_current()
                    self._log.error(
                        "发送状态查询失败",
                        fields={"operation": "poll_send", "reason_code": "MOBILE_SEND_FAILED",
                                "error_type": type(error).__name__},
                    )
                    return {"ok": False, "code": "MOBILE_SEND_FAILED"}
                # 字段名是 `status`(宿主的真实返回:`{"status": "running"}` /
                # `{"status": "completed", "result": {...}}`)。以前读的是 `state`:
                # 永远读到空串 ⇒ 既不匹配 completed 也不匹配 failed ⇒ 一直轮询 ⇒
                # 下一轮撞上 JOB_NOT_FOUND ⇒ 每次发送都报"失败"(真机实测)。
                status = str(state.get("status") or "") if isinstance(state, dict) else ""
                if status == "completed":
                    return {"ok": True, "code": "OK"}
                if status in ("failed", "cancelled"):
                    return {"ok": False, "code": "MOBILE_SEND_FAILED"}
                if self._clock() >= deadline:
                    self.cancel_current()
                    return {"ok": False, "code": "MOBILE_SEND_TIMEOUT"}
                self._sleep(self._poll_interval)
        finally:
            self._active_job = ""
