"""画布绘制层:`CanvasWindow.paint_into` 不建窗口,四条硬性前提在这里锁进仓内。

前提来自 Task 2/3 的裁决(R14/R19):每次重绘必须从新缓冲开始、背景层必须缓存、
`current_segment < 0` 时保持滚动偏移、行定位一律用 `box.top`/`box.height`。
"""

import ctypes
import os
import unittest
from unittest import mock

import _path  # noqa: F401
import canvas
import compose
import layout
import theme
import view
import win32

WINDOWS = os.name == "nt"

SEGMENTS = [
    {"text": "第一句原文", "translation": "第一句译文"},
    {"text": "第二句原文", "translation": "第二句译文"},
    {"text": "第三句原文", "translation": "第三句译文"},
]

LONG_SEGMENTS = [{"text": f"第{i}句原文", "translation": f"第{i}句译文"} for i in range(1, 9)]

# 纯绿 / 纯蓝:预乘后通道特征明显,便于在像素里认出来
HIGHLIGHT = "#FF00FF00"
BORDER = "#FF0000FF"
PADDING_Y = 20  # canvas.PADDING_Y 的镜像;断言里写死数字,改动实现时测试会响


def _theme(**overrides):
    return theme.project_theme(overrides)


def _paint(state, width=None):
    """把视图画进一个新缓冲(与 overlay._render_sample 同一条路径)。"""
    width = int(state.theme_values["width"]) if width is None else int(width)
    boxes = state.boxes(width)
    buffer = compose.new_buffer(width, boxes.view_height + PADDING_Y * 2)
    canvas.CanvasWindow.paint_into(buffer, state, boxes, 0)
    return buffer, boxes


def _highlight_rows(buffer, margin=20):
    """返回「整行大部分是高亮色」的行号(避开圆角与文字对单点采样的干扰)。"""
    rows = []
    columns = buffer.width - margin * 2
    for row in range(buffer.height):
        green = 0
        for column in range(margin, buffer.width - margin):
            index = (row * buffer.width + column) * 4
            b, g, r = buffer.data[index], buffer.data[index + 1], buffer.data[index + 2]
            if g > b + 8 and g > r + 8:
                green += 1
        if green * 2 > columns:
            rows.append(row)
    return rows


@unittest.skipUnless(WINDOWS, "仅 Windows")
class PaintIntoTests(unittest.TestCase):
    def setUp(self):
        canvas._background_cache.clear()  # 缓存是模块级状态,逐个用例清干净

    def test_repaint_must_start_from_clean_state(self):
        """前提①:重绘必须从新缓冲(或显式覆盖)开始,不能累加在旧内容上。"""
        values = _theme()
        first = view.ViewState(values)
        first.set_user("上一次留下的旧文字")
        first.set_reply(SEGMENTS)
        first.set_current(2)
        second = view.ViewState(values)
        second.set_user("这一次的新文字")
        second.set_reply(SEGMENTS)
        second.set_current(0)

        # 两次都画到新缓冲 → 逐字节相同(没有跨次残留)
        one, _ = _paint(first)
        two, _ = _paint(first)
        self.assertEqual(bytes(one.data), bytes(two.data))

        # 就地重绘:先把 first 画进缓冲,再在同一个缓冲上画 second;
        # 结果必须与「新缓冲 + second」逐字节相同 —— 即 render() 的每次新缓冲
        # 与 paint_into 的整块背景层覆盖是等价的两道保险。
        reused = compose.new_buffer(one.width, one.height)
        first_boxes = first.boxes(one.width)
        canvas.CanvasWindow.paint_into(reused, first, first_boxes, 0)
        second_boxes = second.boxes(one.width)
        self.assertEqual(second_boxes.view_height, first_boxes.view_height)  # 两张图必须同尺寸
        canvas.CanvasWindow.paint_into(reused, second, second_boxes, 0)
        fresh, _ = _paint(second)
        self.assertEqual(bytes(reused.data), bytes(fresh.data))

        # 该前提不是空话:填充函数是 source-over,不显式清空就会越叠越不透明
        probe = compose.new_buffer(8, 8)
        compose.fill_rect(probe, 0, 0, 8, 8, (255, 0, 0, 100))
        alpha = probe.data[3]
        compose.fill_rect(probe, 0, 0, 8, 8, (255, 0, 0, 100))
        self.assertGreater(probe.data[3], alpha)

    def test_offset_is_kept_when_no_current_segment(self):
        """前提②:没有当前句时保持上次滚动偏移,不得滚到底部。"""
        values = _theme(maxLines=3)
        state = view.ViewState(values)
        state.set_reply(LONG_SEGMENTS)
        width = int(values["width"])
        state.set_current(5)
        boxes = state.boxes(width)
        self.assertGreater(boxes.max_scroll, 0)
        offset = canvas.CanvasWindow.next_offset(boxes, state.current_segment, 0)
        self.assertGreater(offset, 0)

        state.set_current(None)
        self.assertEqual(state.current_segment, -1)
        self.assertEqual(canvas.CanvasWindow.next_offset(boxes, state.current_segment, offset), offset)

        # 反证:同样的入参喂给 follow_offset(-1) 会命中末尾 notice 行并滚到底部
        state.set_notice("还在回复中", now=0.0)
        pinned = state.boxes(width)
        self.assertGreater(pinned.max_scroll, offset)
        self.assertEqual(layout.follow_offset(pinned, -1, offset), pinned.max_scroll)
        self.assertNotEqual(
            canvas.CanvasWindow.next_offset(pinned, -1, offset), pinned.max_scroll
        )

    def test_highlight_is_clipped_by_rounded_corners(self):
        """高亮底必须被圆角覆盖度裁住,不能在轮廓之外画出像素。"""
        values = _theme(cornerRadius=40, highlightBackground=HIGHLIGHT)
        state = view.ViewState(values)
        state.set_reply(SEGMENTS)  # current_segment = 0:高亮块正好压住上方两个圆角
        buffer, _ = _paint(state)

        reference = _paint_without_highlight()
        escaped = 0
        for index in range(3, len(buffer.data), 4):
            if reference.data[index] == 0 and buffer.data[index] != 0:
                escaped += 1
        self.assertEqual(escaped, 0, "圆角轮廓之外不得出现高亮底像素")

        # 四个角各取 40×40 的方块,逐个定位:其中的「轮廓之外」像素
        # (参考图 alpha == 0)一个都不能被高亮底染上。
        # 注意不能整块判「没有高亮色」—— 圆角方块里本就有轮廓之内的像素,
        # 半径 40 时 (5, 20) 这类像素是半覆盖的合法区域。
        for left, top in ((0, 0), (buffer.width - 40, 0), (0, buffer.height - 40),
                          (buffer.width - 40, buffer.height - 40)):
            corner = 0
            for row in range(top, top + 40):
                for column in range(left, left + 40):
                    index = (row * buffer.width + column) * 4
                    if reference.data[index + 3] == 0 and buffer.data[index + 3] != 0:
                        corner += 1
            self.assertEqual(corner, 0, f"角部 ({left}, {top}) 的轮廓外出现了高亮像素")

    def test_highlight_avoids_the_border_ring(self):
        """有边框时高亮块内缩 borderWidth,边框环上的像素必须还是边框色。"""
        values = _theme(borderWidth=3, borderColor=BORDER, highlightBackground=HIGHLIGHT)
        state = view.ViewState(values)
        state.set_reply(SEGMENTS)
        buffer, boxes = _paint(state)

        block = [box for box in boxes.boxes if box.segment == 0]
        top = min(box.top for box in block) + PADDING_Y
        bottom = max(box.top + box.height for box in block) + PADDING_Y
        border = theme.parse_color(BORDER)
        expected = (border[2], border[1], border[0], border[3])
        for row in range(top, bottom):
            for column in (0, 1, 2):
                index = (row * buffer.width + column) * 4
                self.assertEqual(
                    tuple(buffer.data[index : index + 4]), expected, (column, row)
                )

        # 高亮块本身还在(内缩了,不是整块消失)
        rows = _highlight_rows(buffer)
        self.assertEqual((rows[0], rows[-1]), (top, bottom - 1))

    def test_highlight_block_follows_box_top_with_notice(self):
        """前提④:高亮块纵向位置一律由 box.top/box.height 决定。"""
        values = _theme(highlightBackground=HIGHLIGHT)
        state = view.ViewState(values)
        state.set_reply(SEGMENTS)
        state.set_notice("还在回复中", now=0.0)
        state.set_current(1)
        buffer, boxes = _paint(state)
        block = [box for box in boxes.boxes if box.segment == 1]
        top = min(box.top for box in block) + PADDING_Y
        bottom = max(box.top + box.height for box in block) + PADDING_Y
        self.assertEqual(_highlight_rows(buffer), list(range(top, bottom)))

        # notice 夹在高亮块之前(当前布局不会产出这种盒序,但规则必须成立):
        # notice 行高是 0.8 倍,用行号乘会漂移 5px,用 box.top 不会
        synthetic = layout.LayoutBoxes(
            boxes=[
                layout.LineBox("第一句原文", "source", 0, 0, 27),
                layout.LineBox("还在回复中", "notice", -1, 27, 22),
                layout.LineBox("第二句原文", "source", 1, 49, 27),
                layout.LineBox("第二句译文", "translation", 1, 76, 27),
            ],
            line_height=27,
            content_height=103,
            view_height=103,
            max_scroll=0,
            wrap_width=428,
        )
        other = view.ViewState(values)
        other.set_reply(SEGMENTS)
        other.set_current(1)
        synthetic_buffer = compose.new_buffer(460, synthetic.view_height + PADDING_Y * 2)
        canvas.CanvasWindow.paint_into(synthetic_buffer, other, synthetic, 0)
        rows = _highlight_rows(synthetic_buffer)
        self.assertEqual(rows[0], PADDING_Y + 49)
        self.assertEqual(rows[-1], PADDING_Y + 103 - 1)
        self.assertNotEqual(rows[0], PADDING_Y + 2 * 27)  # 行号乘的错法会落到 74

    def test_background_layer_is_cached_by_theme_and_size(self):
        """前提③:背景层同主题同尺寸复用,主题或尺寸变化时重建。"""
        state = view.ViewState(_theme())
        state.set_reply(SEGMENTS)
        _paint(state)
        self.assertEqual(len(canvas._background_cache), 1)
        key = next(iter(canvas._background_cache))
        cached = canvas._background_cache[key]

        _paint(state)  # 同主题同尺寸
        self.assertEqual(len(canvas._background_cache), 1)
        self.assertIs(canvas._background_cache[key], cached, "应复用同一个背景层对象")

        wider = view.ViewState(_theme(width=380))
        wider.set_reply(SEGMENTS)
        _paint(wider)
        self.assertEqual(len(canvas._background_cache), 2, "尺寸变了要重建")

        rounder = view.ViewState(_theme(cornerRadius=20))
        rounder.set_reply(SEGMENTS)
        _paint(rounder)
        self.assertEqual(len(canvas._background_cache), 3, "主题变了要重建")
        self.assertTrue(all(isinstance(item, canvas._BackgroundLayer)
                            for item in canvas._background_cache.values()))

class PublicEntryPointTests(unittest.TestCase):
    """Task 8 新增的三个公开入口:`reset_scroll` / `restore_default_position` / `close_shared_raster`。

    以前 `overlay` 直接改 `canvas._offset`、`canvas._anchor_bottom`、读 `canvas._shared_raster`,
    canvas 侧一改名就会**静默新建属性/静默跳过收尾**、没有任何测试报警。这三条用例把入口
    钉住:改名(或删掉)就会变红。都不建真窗口 —— `move_to` 被替换成记录器。
    """

    def _fake_window(self):
        window = object.__new__(canvas.CanvasWindow)  # 不跑 __init__:不建窗口、不开 DC
        window._hwnd = 4242
        window._size = (460, 148)
        window._offset = 7
        window._anchor_bottom = False
        return window

    def test_reset_scroll_zeroes_the_offset(self):
        window = self._fake_window()
        window.reset_scroll()
        self.assertEqual(window._offset, 0)

    def test_restore_default_position_uses_default_position_and_reanchors(self):
        window = self._fake_window()
        with mock.patch.object(canvas.CanvasWindow, "move_to") as moved:
            window.restore_default_position()
        moved.assert_called_once_with(*canvas._default_position(460, 148))
        self.assertIs(window._anchor_bottom, True, "回到默认位置后要恢复底边锚点")

    def test_restore_default_position_is_a_noop_after_destroy(self):
        window = self._fake_window()
        window._hwnd = None
        with mock.patch.object(canvas.CanvasWindow, "move_to") as moved:
            window.restore_default_position()
        moved.assert_not_called()

    def test_close_shared_raster_closes_the_shared_one(self):
        canvas._raster()  # 保证存在一个(前面的用例画过的话就是复用它)
        shared = canvas._shared_raster
        self.assertIsNotNone(shared)
        canvas.close_shared_raster()
        self.assertIsNone(canvas._shared_raster)
        self.assertIsNone(shared._memory_dc, "共享光栅器的 DC 要被真的放掉")
        canvas.close_shared_raster()  # 幂等:没有光栅器时什么都不做,也不抛


def _paint_without_highlight():
    """同主题但不设 highlightBackground:轮廓之外 alpha 恒为 0,用作圆角参考图。"""
    state = view.ViewState(_theme(cornerRadius=40))
    state.set_reply(SEGMENTS)
    buffer, _ = _paint(state)
    return buffer


class DragTests(unittest.TestCase):
    """拖动必须用**屏幕**坐标算增量(真机 bug:`报告`窗口只走到鼠标位移的一半并来回抽搐)。

    这条用例把 Windows 的填值规则也模拟进来,否则咬不住这个 bug:
    - 窗口位置由 `move_to` 记录 —— 窗口真的在动,`_window_rect()` 读到的就是它;
    - 每条 `WM_MOUSEMOVE` 的 lparam 按「光标屏幕坐标 − **当前**窗口位置」现算 ——
      Windows 就是这么填客户端坐标的:窗口自己刚走过的位移会在这里被扣掉一次;
    - `ClientToScreen` 注入桩,按同一个当前窗口位置换回屏幕坐标(不碰真 Win32)。

    旧实现(拿客户端坐标做增量)在这一串输入下第一步对、第二步起就停在原地并来回抖:
    `新位置 = 按下位置 + 鼠标屏幕 − 窗口当前位置 − 按下时客户端坐标`。逐步断言让它立刻红。
    """

    PRESS_CURSOR = (1200, 900)
    WINDOW_AT = (1000, 800)
    WINDOW_SIZE = (460, 148)

    def _make(self):
        window = object.__new__(canvas.CanvasWindow)  # 不跑 __init__:不建窗口、不开 DC
        window._hwnd = 4242
        window._dragging = False
        window._drag_origin = (0, 0)
        window._drag_window = (0, 0)
        window._tracking_leave = True  # 悬停那半不参与:别让 TrackMouseEvent 真跑
        window._on_hover_change = None
        window._on_drag_start = None
        window._on_drag_end = None

        state = {"pos": self.WINDOW_AT}
        window.move_to = lambda x, y: state.__setitem__("pos", (x, y))
        window._window_rect = lambda: (*state["pos"], *self.WINDOW_SIZE)

        def client_to_screen(_hwnd, point_ref):
            # 真实换算:屏幕坐标 = 窗口左上角 + 客户端坐标
            point = ctypes.cast(point_ref, ctypes.POINTER(win32.POINT)).contents
            point.x += state["pos"][0]
            point.y += state["pos"][1]
            return 1

        patcher = mock.patch.object(win32.user32, "ClientToScreen", client_to_screen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return window, state

    def _lparam(self, cursor: tuple[int, int], window_pos: tuple[int, int]) -> int:
        """按 Windows 的规则算 lparam:客户端坐标 = 光标屏幕坐标 − 窗口**当前**位置。"""
        x = cursor[0] - window_pos[0]
        y = cursor[1] - window_pos[1]
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def test_drag_follows_the_cursor_screen_delta(self):
        window, state = self._make()
        cursors = [
            (1200, 900),  # 按下那一点
            (1210, 890),  # 往右下走
            (1240, 860),
            (1230, 900),  # 掉头往回:旧实现正是在这种反复里抽搐
            (1180, 830),
        ]
        with mock.patch.object(win32.user32, "SetCapture"), \
             mock.patch.object(win32.user32, "ReleaseCapture"):
            window._begin_drag(self._lparam(self.PRESS_CURSOR, state["pos"]))
            for cursor in cursors:
                window._on_mouse_move(self._lparam(cursor, state["pos"]))
                expected = (
                    self.WINDOW_AT[0] + cursor[0] - self.PRESS_CURSOR[0],
                    self.WINDOW_AT[1] + cursor[1] - self.PRESS_CURSOR[1],
                )
                self.assertEqual(state["pos"], expected,
                                 f"光标在 {cursor} 时窗口要精确跟随(不是走一半、也不是抖)")
            window._end_drag()

    def test_drag_callbacks_bracket_the_session(self):
        """`on_drag_start` 在按下时触发、`on_drag_end` 在松开时带上最终位置。

        overlay 靠这两个回调冻结悬停与输入框(拖动中不许显隐),少一个就回到"闪"。
        """
        window, state = self._make()
        events = []
        window._on_drag_start = lambda: events.append("start")
        window._on_drag_end = lambda x, y: events.append(("end", x, y))
        with mock.patch.object(win32.user32, "SetCapture"), \
             mock.patch.object(win32.user32, "ReleaseCapture"):
            window._begin_drag(self._lparam(self.PRESS_CURSOR, state["pos"]))
            self.assertEqual(events, ["start"], "按下就要通知(overlay 立刻开始冻结)")
            window._on_mouse_move(self._lparam((1250, 830), state["pos"]))
            window._end_drag()
        self.assertEqual(events, ["start", ("end", 1050, 730)])
        window._end_drag()  # 幂等:没在拖动时不再上报
        self.assertEqual(events, ["start", ("end", 1050, 730)])

    def test_capture_loss_ends_the_drag(self):
        """捕获被抢走(或窗口被隐藏)后不该继续"拖着走":`_dragging` 必须落回 False。

        不落回的话,之后的 WM_MOUSEMOVE(没有按键)会继续把窗口搬来搬去,而
        `on_drag_end` 永远不来 —— 栏黏在光标上,overlay 那边的悬停也解不了冻。
        """
        window, state = self._make()
        events = []
        window._on_drag_end = lambda x, y: events.append(("end", x, y))
        with mock.patch.object(win32.user32, "SetCapture"), \
             mock.patch.object(win32.user32, "ReleaseCapture"):
            window._begin_drag(self._lparam(self.PRESS_CURSOR, state["pos"]))
            window._handle(4242, win32.WM_CAPTURECHANGED, 0, 0)
        self.assertFalse(window._dragging)
        self.assertEqual(events, [("end", *self.WINDOW_AT)])
        kept = state["pos"]
        window._on_mouse_move(self._lparam((1400, 700), state["pos"]))
        self.assertEqual(state["pos"], kept, "拖动结束后鼠标移动不该再搬窗口")


class HighlightTextColorTests(unittest.TestCase):
    """当前句的高亮**文字色**必须落在它实际显示出来的行上。

    背景高亮(highlightBackground)可以留空 —— 那时文字色就是唯一的高亮提示。
    所以三种显示模式都必须能看见高亮色;只中文模式下没有原文行,高亮色必须落到译文行。
    """

    HIGHLIGHT_COLOR = "#FFFF0000"  # 纯红:与白字(#FFFFFF)、灰字(#CFCFCF)一眼分开

    def _reddish(self, buffer):
        count = 0
        for index in range(0, len(buffer.data), 4):
            blue, green, red, alpha = buffer.data[index:index + 4]
            if alpha > 40 and red > green + 40 and red > blue + 40:
                count += 1
        return count

    def _paint_mode(self, mode):
        values = _theme(
            mode=mode,
            highlightColor=self.HIGHLIGHT_COLOR,
            textColor="#FFFFFF",
            translationColor="#CFCFCF",
            backgroundOpacity=100,
            overallOpacity=100,
        )
        state = view.ViewState(values)
        state.set_reply(SEGMENTS)
        state.set_current(0)
        buffer, _boxes = _paint(state)
        return buffer

    def test_bilingual_highlights_a_line(self):
        self.assertGreater(self._reddish(self._paint_mode("bilingual")), 0)

    def test_translation_only_mode_still_highlights(self):
        # 只中文:画面上没有原文行,高亮色必须落到译文行,否则用户完全看不到高亮
        self.assertGreater(
            self._reddish(self._paint_mode("zh")), 0,
            "只显示译文时没有任何高亮色 —— 当前句的高亮完全失效",
        )

    def test_source_only_mode_highlights(self):
        self.assertGreater(self._reddish(self._paint_mode("ja")), 0)


class RenderUsesFontMetricsTests(unittest.TestCase):
    """`render` 必须把字体度量交给布局,并按**实测折行高度**排版。

    否则长台词折行后仍然只占一个固定行高:多出来的行压到下一句身上、或被栏底裁掉。
    """

    LONG = "很长很长的一句中文台词,用来测试折行后的高度会不会被正确算进布局,这条应该会折好几行。"

    def test_render_measures_wrapped_line_height(self):
        values = _theme(mode="zh", width=300, fontSize=18, maxLines=20)
        state = view.ViewState(values)
        state.set_reply([{"text": "", "translation": self.LONG}])
        window = canvas.CanvasWindow(values)
        window._hwnd = 1  # 测试替身:只为让 render 不早退,窗口本身由下面几个 mock 顶掉
        captured = {}

        def capture(_buffer, _view, boxes, _offset):
            captured["boxes"] = boxes

        with mock.patch.object(canvas.CanvasWindow, "_resize"), \
                mock.patch.object(canvas.CanvasWindow, "_upload"), \
                mock.patch.object(canvas.CanvasWindow, "_apply_visibility"), \
                mock.patch.object(canvas.CanvasWindow, "paint_into", staticmethod(capture)):
            window.render(state)

        boxes = captured["boxes"]
        self.assertGreater(
            boxes.boxes[0].height, boxes.line_height,
            "render 没把字体度量接进布局:长台词折行后仍按固定行高排版",
        )
        self.assertGreaterEqual(boxes.view_height, boxes.boxes[0].height)

    def test_empty_bar_strip_is_tall_enough_to_grab(self):
        """空栏(刚启动 / 换角色后)收起的细条是唯一的拖拽把手 —— 不能细到抓不住。"""
        values = _theme(mode="zh", width=460, idleCollapse=True)
        state = view.ViewState(values)  # 完全没有内容
        window = canvas.CanvasWindow(values)
        window._hwnd = 1
        heights = []

        with mock.patch.object(canvas.CanvasWindow, "_resize",
                               lambda self, w, h: heights.append(h)), \
                mock.patch.object(canvas.CanvasWindow, "_upload"), \
                mock.patch.object(canvas.CanvasWindow, "_apply_visibility"), \
                mock.patch.object(canvas.CanvasWindow, "paint_into", staticmethod(lambda *a: None)):
            window.render(state)

        self.assertTrue(heights, "空栏时应该仍然渲染出那条细条")
        self.assertGreaterEqual(heights[0], 12, "空栏细条太细,鼠标抓不住")

    def test_short_line_keeps_one_line_height(self):
        values = _theme(mode="zh", width=460, fontSize=18, maxLines=20)
        state = view.ViewState(values)
        state.set_reply([{"text": "", "translation": "短句"}])
        boxes = state.boxes(460, canvas.theme_measure(values))
        self.assertLessEqual(boxes.boxes[0].height, boxes.line_height)


if __name__ == "__main__":
    unittest.main()
