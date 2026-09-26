"""Task 13 的硬契约回归锁 —— 冻结的 `test_plugin_wiring.py` 咬不住的那些。

为什么另开一个文件:`tests/test_plugin_wiring.py` 是本任务书**逐字冻结**的,而它一条硬契约
都没钉住 —— 把 `save=runtime.save_config` 改回 `handle_config_change`、删掉 `_advance(index)`
的调用、或删掉 `_init_cursor()`,那 11 个用例**照样全绿**(评审实测)。那些契约是前面十几个
任务用真实证据换来的,不能没有回归保护,所以本文件逐条对着"会静默失效的接线"写:

- `CursorInitTests` —— 启动时钉住 cursor(少了它,启动后的第一条回复永不上屏);
- `ContractThreeTests` —— `-1` ⇒ 推 `None`;`None` ⇒ 一条都不推;
- `ContractFiveTests` —— `hasMore` 续读,且 cursor 不前进时必须停;
- `ContractSixTests` —— `restartOverlay` 的三个分支;
- `ContractSevenTests` —— `current_font` 必须是拆分后的真实字体名;
- `SaveConfigTests` —— `save` 必须自己持久化(宿主不会替插件写盘),且不能递归;
- `SetupWiringTests` —— `setup()` 真的把上面这些东西接上去了。

每条用例都 `addCleanup(runtime.stop)`:运行时自己起 `lyric-bar-write` 守护线程,不调 `stop()`
就会一个 runtime 漏一个线程(冻结块从不调它)。
"""

import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401
import plugin
import settings_schema

CHAT_COMPLETED = "sakura.host.chat.completed"
TTS_STARTED = "sakura.host.tts.started"
TTS_ENDED = "sakura.host.tts.ended"


class _Logger:
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


class _Config:
    """宿主 `PluginConfig` 的等价形状:update 写盘**后**才回调 on_change,并拿它的返回值定结果。

    `depth` 用来抓递归:宿主 `_write()` 把 `_applied_config` 置 None 之后才调处理器,处理器里
    再 `update()` 一次就是无限嵌套(真实宿主实测 248 层 → RecursionError)。
    """

    def __init__(self, values=None, result=None):
        self.values = dict(values or {})
        self.handlers = []
        self.updates = []
        self.depth = 0
        self.max_depth = 0
        self.result = result

    def get(self):
        return dict(self.values)

    def update(self, values):
        self.updates.append(dict(values))
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            self.values.update(values)
            results = [handler(dict(self.values)) for handler in list(self.handlers)]
        finally:
            self.depth -= 1
        if self.result:
            return self.result
        if "error" in results:
            return "error"
        if "restart_required" in results:
            return "restart_required"
        return "applied"

    def on_change(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)


class _Timeline:
    """可编排的假时间线:`pages` 有货就按顺序出页,否则回一页 hasMore=False。"""

    def __init__(self):
        self.entries = []
        self.pages = []
        self.recent_cursor = "recent-cursor"
        self.requests = []

    def read_recent(self, request):
        self.requests.append(("recent", dict(request)))
        return {"entries": list(self.entries), "cursor": self.recent_cursor}

    def read_since(self, request):
        self.requests.append(("since", dict(request)))
        if self.pages:
            return self.pages.pop(0)
        return {"entries": list(self.entries), "nextCursor": request.get("cursor", ""), "hasMore": False}


class _Link:
    """假链路:`start()` 无参、`send()` 收消息、`stop()` 幂等。"""

    def __init__(self):
        self.sent = []
        self.running = False
        self.started = 0
        self.fail_start = False

    def start(self):
        self.started += 1
        if self.fail_start:
            return False
        self.running = True
        return True

    def send(self, message):
        self.sent.append(message)
        return True

    def stop(self):
        self.running = False


class _Mobile:
    def characters(self):
        return [{"id": "tian", "name": "天", "current": "true"}]

    def begin(self, plugin_id, character_id, text, artifact):
        return {"jobId": "j1"}

    def poll(self, plugin_id, job_id):
        # 同 test_plugin_wiring:宿主的字段名是 `status`(mobile_host.py:236-244)
        return {"status": "completed"}

    def cancel(self, plugin_id, job_id):
        return None


def _assistant(segments, entry_id="e1"):
    return {"entryId": entry_id, "kind": "assistant", "characterId": "tian",
            "payload": {"segments": segments}}


def _segment(text, translation=""):
    return {"text": text, "translation": translation}


def _status(label):
    return {"state": "neutral", "label": label, "message": ""}


class _RuntimeCase(unittest.TestCase):
    """公共脚手架:建运行时并保证收尾(写线程是 daemon,不收就漏)。"""

    def make_runtime(self, values=None, timeline=None, link=None, config=None):
        timeline = timeline if timeline is not None else _Timeline()
        link = link if link is not None else _Link()
        config = config if config is not None else _Config(values)
        runtime = plugin.LyricBarRuntime(
            context=None, plugin_dir="", log=_Logger(),
            timeline=timeline, mobile=_Mobile(), config=config,
            overlay_link=link, clock=lambda: 100.0,
        )
        self.addCleanup(runtime.stop)
        return runtime


class CursorInitTests(_RuntimeCase):
    """启动时必须先读一次 `read_recent({limit: 1})` 把 cursor 钉在"此刻最新"。

    少了这一步,第一次 `chat.completed` 会退回去用事件自带的 cursor —— 那是
    `timeline.latest_cursor()`(这一轮自己最后一条),而 `read_since` 是 `seq >` 的**排他**
    读取 ⇒ **这一轮读不到,启动后的第一条回复永不上屏**。
    """

    def test_start_pins_the_cursor_from_recent(self):
        runtime = self.make_runtime()
        runtime.start()
        self.assertEqual(runtime.timeline.requests[0], ("recent", {"limit": 1}),
                         "启动要先读一次 recent(limit=1):只定位置,不重放历史")
        self.assertEqual(runtime.last_cursor, "recent-cursor")

        runtime.timeline.entries = [_assistant([_segment("やあ", "嗨")])]
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "event-cursor"})
        since = [request for kind, request in runtime.timeline.requests if kind == "since"]
        self.assertTrue(since, "必须走 read_since")
        self.assertEqual(since[-1]["cursor"], "recent-cursor",
                         "要用启动时钉住的 cursor;用事件自带的 cursor 会读不到这一轮")
        self.assertEqual(runtime.overlay.sent[-1]["type"], "reply")

    def test_first_empty_batch_pushes_nothing(self):
        # 契约 2 的意图:`segments` 初值是 `[]`,所以"还没有任何 assistant 条目"的首批
        # 不该被当成"变过了"而推一条空 reply + current:None 把屏幕清掉。
        runtime = self.make_runtime()
        runtime.start()
        sent = list(runtime.overlay.sent)
        runtime.timeline.entries = []
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "event-cursor"})
        self.assertEqual(runtime.overlay.sent, sent, "空批没有任何内容可推")


class ContractThreeTests(_RuntimeCase):
    """契约 3:`-1` 必须转成 `None` 推出去;`None` 是"没变化",一条都不推。"""

    def test_begin_turn_minus_one_pushes_null(self):
        runtime = self.make_runtime({"syncMode": "off"})
        runtime.start()
        runtime.timeline.entries = [_assistant([_segment("一"), _segment("二")])]
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        self.assertEqual([message["type"] for message in runtime.overlay.sent[-2:]], ["reply", "current"],
                         "清零必须排在 reply 之后 —— 在前会被 set_reply 顶成「第 1 句高亮」")
        self.assertEqual(runtime.overlay.sent[-1], {"type": "current", "index": None},
                         "-1 要转成 None:view.set_current(-1) 会被夹到 0")

    def test_none_from_tts_and_poll_pushes_nothing(self):
        runtime = self.make_runtime()
        runtime.start()
        runtime.timeline.entries = [_assistant([_segment("一"), _segment("二")])]
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        runtime.on_host_event(TTS_STARTED, {"outcome": "started"})  # 真正推进:推一次
        self.assertEqual(runtime.overlay.sent[-1], {"type": "current", "index": 1})

        sent = list(runtime.overlay.sent)
        runtime.on_host_event(TTS_STARTED, {"outcome": "stopped"})  # on_tts_started 返回 None
        runtime.on_host_event(TTS_ENDED, {"outcome": "finished"})
        runtime.tick()  # poll 返回 None
        self.assertEqual(runtime.overlay.sent, sent, "None 表示「没变化」,不是「清零」")


class ContractFiveTests(_RuntimeCase):
    """契约 5:`hasMore=True` 时被丢的是**最新**条目,必须按 nextCursor 续读到尾。"""

    def test_reads_through_to_the_newest_page(self):
        runtime = self.make_runtime()
        runtime.start()
        runtime.timeline.pages = [
            {"entries": [_assistant([_segment("旧句")], entry_id="e1")],
             "nextCursor": "page-1", "hasMore": True},
            {"entries": [_assistant([_segment("最新句")], entry_id="e2")],
             "nextCursor": "page-2", "hasMore": False},
        ]
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        since = [request for kind, request in runtime.timeline.requests if kind == "since"]
        self.assertEqual(len(since), 2, "hasMore=True 时必须接着往后读")
        self.assertEqual(runtime.overlay.sent[-1],
                         {"type": "reply", "segments": [_segment("最新句")]},
                         "推上屏的必须是**最新**那一页")

    def test_a_stuck_cursor_stops(self):
        runtime = self.make_runtime()
        runtime.start()
        stuck = {"entries": [_assistant([_segment("重复")])], "nextCursor": "stuck", "hasMore": True}
        runtime.timeline.pages = [dict(stuck) for _ in range(12)]
        runtime.on_host_event(CHAT_COMPLETED, {"characterId": "tian", "turnId": "t1", "cursor": "c9"})
        since = [request for kind, request in runtime.timeline.requests if kind == "since"]
        # 1 次发现 hasMore、1 次发现 cursor 没往前挪 —— 之后必须停,既不能无限读,
        # 也不能靠"最多 5 轮"的保险丝一路读满。
        self.assertEqual(len(since), 2, "cursor 不前进时必须立刻停")


class ContractSixTests(_RuntimeCase):
    """契约 6:`enabledWhen` 被宿主丢弃,所以可用性自己判,返回消息要说清做了什么。"""

    def test_not_running_only_starts(self):
        runtime = self.make_runtime()
        runtime.start()
        runtime.overlay.running = False
        self.assertEqual(runtime.handle_action("restartOverlay", {}), {"message": "浮窗已启动"})
        self.assertEqual(runtime.overlay.sent[-1]["type"], "hello", "新进程要重新握手")

    def test_running_restarts(self):
        runtime = self.make_runtime()
        runtime.start()
        self.assertTrue(runtime.overlay.running)
        self.assertEqual(runtime.handle_action("restartOverlay", {}), {"message": "浮窗已重启"})
        self.assertEqual(runtime.overlay.started, 2, "stop() 之后要真的再 start() 一次")

    def test_start_failure_is_reported(self):
        runtime = self.make_runtime()
        runtime.start()
        runtime.overlay.fail_start = True
        self.assertEqual(runtime.handle_action("restartOverlay", {}), {"message": "浮窗启动失败"})


class ContractSevenTests(unittest.TestCase):
    """契约 7:合并名(带 ` & `)不能在 select 选项里,而 select 的 default 必须落在选项内。"""

    def test_merged_current_font_is_split_and_the_default_stays_selectable(self):
        descriptor = settings_schema.build_descriptor(
            ["Arial"], _status("未运行"), _status("默认位置"),
            current_font="Microsoft YaHei & Microsoft YaHei UI",
        )
        field = next(item for item in descriptor["fields"] if item["key"] == "fontFamily")
        values = [option["value"] for option in field["options"]]
        self.assertTrue(all(" & " not in value for value in values), values)
        self.assertIn(field["default"], values, "select 的 default 不在 options 里会被宿主丢掉整个字段")
        self.assertIn("Microsoft YaHei", values, "合并名要拆成真实字体名")
        self.assertIn("Microsoft YaHei UI", values)


class SaveConfigTests(_RuntimeCase):
    """`save` 与 `on_change` 的分离:宿主**不**替插件写盘(`plugin_host_services.py` 的
    `save()` 只调回调看返回的状态),所以 save 必须自己 `config.update()`;而 update 会回调
    on_change —— 处理器里再 update 一次就是无限递归(真实宿主 248 层 → RecursionError)。"""

    def test_save_persists_and_applies_the_merged_config(self):
        config = _Config({"fontSize": 20, "textColor": "#FF0000"})
        runtime = self.make_runtime(config=config)
        runtime.start()
        config.on_change(runtime.handle_config_change)  # setup 的第 8 步

        self.assertEqual(runtime.save_config({"fontSize": 26}), "applied")
        self.assertEqual(len(config.updates), 1, "save 必须调一次 config.update(宿主不会替它写盘)")
        self.assertEqual(config.max_depth, 1, "on_change 处理器里不能再 update(无限递归)")
        theme_values = runtime.overlay.sent[-1]["theme"]
        self.assertEqual(theme_values["fontSize"], 26)
        self.assertEqual(theme_values["textColor"], "#FF0000",
                         "没提交的字段不能被表单子集打回默认值")

    def test_save_passes_the_host_state_through(self):
        config = _Config({"fontSize": 20}, result="restart_required")
        runtime = self.make_runtime(config=config)
        runtime.start()
        self.assertEqual(runtime.save_config({"fontSize": 26}), "restart_required",
                         "宿主的状态要透传,不能写死 applied")


class _Settings:
    def __init__(self):
        self.registered = {}

    def register(self, descriptor, load=None, save=None, actions=None):
        self.registered = {"descriptor": descriptor, "load": load, "save": save,
                           "actions": dict(actions or {})}
        return lambda: None


class _HostContext:
    """宿主形状的 context:`get` 发服务、`on` 收事件、`effect` 收清理、`config` 是配置服务。"""

    def __init__(self, root, values=None):
        self.plugin_id = "local.sakura.lyric-bar"
        self.config = _Config(values)
        self.logging = _Logger()
        self.settings = _Settings()
        self.timeline = _Timeline()
        self.mobile = _Mobile()
        self.events = []
        self.effects = []
        self._root = root

    def get(self, key):
        return {
            "sakura.host.logging": self.logging,
            "sakura.host.settings": self.settings,
            "sakura.host.timeline": self.timeline,
            "sakura.host.mobile": self.mobile,
        }.get(key)

    def on(self, name, handler):
        self.events.append((name, handler))

    def effect(self, cleanup):
        self.effects.append(cleanup)

    def data_path(self, relative):
        return self._root / relative


class SetupWiringTests(unittest.TestCase):
    """`setup()` 有没有把契约真正接上去(评审的变异:把 `save=save_config` 改回去,191/191 全绿)。"""

    def setup_plugin(self, **values):
        context = _HostContext(tempfile.mkdtemp(), values)
        link = _Link()
        # setup() 会真的拉起子进程 —— 这里换成假链路(只在测试里)
        with mock.patch.object(plugin.hostlink, "OverlayLink", lambda *args, **kwargs: link):
            plugin.LyricBarPlugin().setup(context)
        self.addCleanup(lambda: [cleanup() for cleanup in reversed(context.effects)])
        return context, link

    def test_setup_subscribes_events_and_registers_every_action(self):
        context, link = self.setup_plugin(fontFamily="Arial", width=500)
        self.assertEqual([message["type"] for message in link.sent][:1], ["hello"])
        self.assertEqual(sorted(name for name, _handler in context.events),
                         [CHAT_COMPLETED, TTS_ENDED, TTS_STARTED])
        registration = context.settings.registered
        self.assertEqual(sorted(registration["actions"]), sorted(settings_schema.ACTION_IDS),
                         "每个声明的 Action 都必须有同名回调")
        field = next(item for item in registration["descriptor"]["fields"] if item["key"] == "fontFamily")
        # 契约 7 的接线面:传的必须是 project_theme 之后的真名 —— `_font_options` 把它排首位
        self.assertEqual(field["options"][0]["value"], "Arial")

    def test_setup_save_persists_through_config_update(self):
        context, link = self.setup_plugin(fontSize=20, textColor="#FF0000")
        self.assertEqual(context.settings.registered["save"]({"fontSize": 26}), "applied")
        self.assertEqual(len(context.config.updates), 1,
                         "注册的 save 必须是会写盘的 save_config,而不是只应用的 handle_config_change")
        theme_values = link.sent[-1]["theme"]
        self.assertEqual((theme_values["fontSize"], theme_values["textColor"]), (26, "#FF0000"))


if __name__ == "__main__":
    unittest.main()
