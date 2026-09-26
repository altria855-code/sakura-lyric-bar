"""`--run` 的显示策略与消息分派:用不建窗口的对象骨架验证接线。

覆盖 Task 8 评审的两个 Important 里的第一个(全屏开关)与第 4 项(主循环宽兜异常):
- `hideInFullscreen` 必须真的被读到 —— 关掉它以后,前台全屏也不该隐藏(验收清单第 11 条);
- 单条消息处理抛异常时,`_dispatch_all` 要活下来并把它后面的消息照常处理掉。

这两条以前都没有测试咬得住:`_policy_visible` 漏读主题字段、`_dispatch_all` 完全不兜。
"""

import unittest
from unittest import mock

import _path  # noqa: F401
import overlay
import theme
import view


def _session(**theme_overrides) -> overlay._RunSession:
    """搭一个**不建窗口**的会话骨架:只放策略判定与分派需要的字段。

    用 `object.__new__` 跳过 `__init__`(那里会真建两个窗口),其余字段按需补上。
    """
    session = object.__new__(overlay._RunSession)
    session.view = view.ViewState(theme.project_theme(theme_overrides))
    session.hide_by_policy = False
    session.fullscreen_active = False
    session._dirty = False
    session._warned = {}
    return session


def _channel(*messages) -> overlay._Channel:
    """不接管道的 _Channel:`__init__` 不碰 stdio,直接往队列里塞消息即可。"""
    channel = overlay._Channel()
    for message in messages:
        channel.messages.put(message)
    return channel


class PolicyVisibilityTests(unittest.TestCase):
    def test_default_theme_is_visible(self):
        self.assertTrue(_session()._policy_visible())

    def test_fullscreen_hides_when_the_switch_is_on(self):
        session = _session()  # 主题默认 hideInFullscreen=True
        session.fullscreen_active = True
        self.assertFalse(session._policy_visible())

    def test_fullscreen_does_not_hide_when_the_switch_is_off(self):
        # 这条钉住「hideInFullscreen 真的接线」:漏读主题字段时它会红
        session = _session(hideInFullscreen=False)
        session.fullscreen_active = True
        self.assertTrue(session._policy_visible())

    def test_enabled_false_hides_even_without_fullscreen(self):
        self.assertFalse(_session(enabled=False)._policy_visible())

    def test_hide_message_hides_without_fullscreen(self):
        session = _session()
        session.hide_by_policy = True
        self.assertFalse(session._policy_visible())


class DragFreezeTests(unittest.TestCase):
    """拖动期间:悬停冻住 + 输入框不出现、不跟随(真机:拖着拖着输入框反复抽搐)。

    依赖 canvas 的 `on_drag_start`/`on_drag_end` 两个回调;这里用不建窗口的骨架验证
    overlay 这一侧的策略判定,消息路径(投鼠标消息)由 test_canvas_paint.DragTests 钉。
    """

    CANVAS_RECT = (1000, 800, 460, 148)

    def _dragging_session(self):
        session = _session()
        session.channel = mock.Mock()  # 出站消息只记调用,不写真 stdout
        session.canvas = mock.Mock()
        session.canvas.geometry.return_value = self.CANVAS_RECT
        session.inputbox = mock.Mock()
        session._hover_active = False
        session._hover_deadline = None
        session._attached_key = None
        session._drag_active = False
        session._drag_pressed_rect = None
        session._drag_hid_box = False
        # 报位里的显示器查询不参与本组用例:别去碰真 EnumDisplayMonitors
        patcher = mock.patch.object(overlay, "_screen_id_at", return_value="PRIMARY")
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_hover_is_frozen_while_dragging(self):
        session = self._dragging_session()
        session._on_drag_start()
        session._on_hover(True)
        self.assertFalse(session._hover_active, "拖动中悬停状态不动")
        self.assertIsNone(session._hover_deadline)
        session.inputbox.show.assert_not_called()
        session._on_hover(False)
        self.assertIsNone(session._hover_deadline, "拖动中不起收起计时(否则拖完就没框了)")

    def test_box_is_hidden_and_does_not_follow(self):
        session = self._dragging_session()
        session._on_drag_start()
        session.canvas.geometry.return_value = (1010, 790, 460, 148)  # 栏被拖走了
        session._sync_box()
        session.inputbox.attach.assert_not_called()
        session.inputbox.hide.assert_called_once()
        self.assertTrue(session._drag_hid_box)
        self.assertIsNone(session._attached_key, "拖完必须重贴一次(别让 old key 短路掉)")

    def test_a_click_without_movement_leaves_the_box_alone(self):
        """单点一下栏(按下-抬起、位置没变)不算拖动:不许把输入框藏一下就放回来。"""
        session = self._dragging_session()
        session._on_hover(True)
        session.inputbox.reset_mock()
        session._on_drag_start()  # 按下
        session._sync_box()       # 还没动
        session._on_drag_end(*self.CANVAS_RECT[:2])  # 抬起
        session.inputbox.hide.assert_not_called()
        session.inputbox.show.assert_not_called()

    def test_release_on_the_bar_brings_the_box_back(self):
        session = self._dragging_session()
        session._on_drag_start()
        session.canvas.geometry.return_value = (1010, 790, 460, 148)
        session._sync_box()  # 拖动中:藏起来
        session.inputbox.reset_mock()
        with mock.patch.object(overlay, "_cursor_inside", return_value=True):
            session._on_drag_end(1010, 790)
        self.assertFalse(session._drag_active)
        self.assertTrue(session._hover_active)
        session.inputbox.attach.assert_called_once()  # 新几何要立刻贴上
        session.inputbox.show.assert_called_once()

    def test_release_off_the_bar_keeps_it_hidden(self):
        session = self._dragging_session()
        session._on_drag_start()
        session.canvas.geometry.return_value = (1010, 790, 460, 148)
        session._sync_box()
        session.inputbox.reset_mock()
        with mock.patch.object(overlay, "_cursor_inside", return_value=False):
            session._on_drag_end(1010, 790)
        session.inputbox.show.assert_not_called()
        self.assertFalse(session._hover_active)
        self.assertIsNotNone(session._hover_deadline, "离开栏就该起收起计时")

    def test_the_moved_message_still_goes_out(self):
        # 拖动本身的报位不能被冻结顺带弄丢
        session = self._dragging_session()
        session._on_drag_start()
        session.canvas.geometry.return_value = (1010, 790, 460, 148)
        session._sync_box()
        with mock.patch.object(overlay, "_cursor_inside", return_value=True):
            session._on_drag_end(1010, 790)
        session.channel.send.assert_called_once_with(
            {"type": "moved", "x": 1010, "y": 790, "screenId": "PRIMARY"})


class DispatchTests(unittest.TestCase):
    def test_bye_stops_the_loop(self):
        session = _session()
        session.channel = _channel({"type": "bye"})
        self.assertFalse(session._dispatch_all())

    def test_loop_body_exception_does_not_kill_the_loop(self):
        """主循环层的兜底:一圈里抛出的意外异常只记日志,循环继续转(下一圈照常 pump)。"""
        session = _session()
        session.channel = _channel()
        session.canvas = mock.Mock()
        session.canvas.pump.side_effect = [True, False]  # 第二圈才正常退出
        session.inputbox = mock.Mock()
        with mock.patch.object(session, "_startup"), \
             mock.patch.object(session, "_shutdown"), \
             mock.patch.object(session, "_tick", side_effect=RuntimeError("炸了")), \
             mock.patch.object(overlay, "_warn") as warned:
            exit_code = session.run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(session.canvas.pump.call_count, 2, "异常之后循环还得继续转")
        logged = [call.args[0] for call in warned.call_args_list]
        self.assertTrue(any("OVERLAY_LOOP_ERROR" in text for text in logged), f"要记日志:{logged}")

    def test_failing_handler_is_logged_and_the_drain_continues(self):
        session = _session()
        session.channel = _channel(
            {"type": "reply", "segments": [{"text": "boom", "translation": ""}]},
            {"type": "user", "text": "后面的消息要照常处理"},
        )
        with mock.patch.object(session.view, "set_reply", side_effect=RuntimeError("炸了")):
            with mock.patch.object(overlay, "_warn") as warned:
                self.assertTrue(session._dispatch_all(), "坏消息不该让主循环退出")
        self.assertEqual(session.view.user_text, "后面的消息要照常处理",
                         "坏消息后面的消息仍要被处理(不是整个 drain 一起丢)")
        logged = [call.args[0] for call in warned.call_args_list]
        self.assertTrue(any("OVERLAY_DISPATCH_FAILED" in text for text in logged),
                        f"异常要记日志:{logged}")


if __name__ == "__main__":
    unittest.main()
