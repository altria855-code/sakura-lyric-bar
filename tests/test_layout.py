import unittest

import _path  # noqa: F401
import layout
import theme

SEGMENTS = [
    {"text": "おはよう、今日もいい天気だね。", "translation": "早上好,今天天气也不错呢。"},
    {"text": "ちょっと待ってて。", "translation": "稍等一下。"},
]


def make_theme(**overrides):
    values = {"fontSize": 20, "lineSpacing": 1.5, "maxLines": 4, "mode": "bilingual"}
    values.update(overrides)
    return theme.project_theme(values)


class MeasuredHeightTests(unittest.TestCase):
    """长台词会在渲染时折行 —— 折行后的真实高度必须进布局,否则文字压到下一句或被裁掉。

    布局层是纯计算,不知道字体度量,所以由调用方注入 `measure(文字, 最大宽度) -> 高度`。
    """

    def test_without_measure_every_line_is_one_line_height(self):
        lines = layout.build_lines([{"text": "", "translation": "很长的一句"}], "zh", "", "")
        box = layout.compute_layout(lines, make_theme(), 460)
        self.assertEqual(box.boxes[0].height, box.line_height)

    def test_wrapped_line_uses_measured_height(self):
        lines = layout.build_lines(
            [{"text": "", "translation": "这句很长会被折成三行"}], "zh", "", ""
        )
        box = layout.compute_layout(lines, make_theme(), 460, measure=lambda text, width: 90)
        self.assertEqual(box.boxes[0].height, 90)
        self.assertEqual(box.content_height, 90)

    def test_measured_heights_stack_and_push_down(self):
        lines = layout.build_lines(
            [{"text": "", "translation": "短"}, {"text": "", "translation": "很长很长的一句"}],
            "zh", "", "",
        )
        box = layout.compute_layout(
            lines, make_theme(), 460, measure=lambda text, width: 90 if len(text) > 2 else 30
        )
        self.assertEqual([b.height for b in box.boxes], [30, 90])
        self.assertEqual([b.top for b in box.boxes], [0, 30])
        self.assertEqual(box.content_height, 120)

    def test_measure_that_raises_falls_back_to_one_line(self):
        lines = layout.build_lines([{"text": "", "translation": "随便"}], "zh", "", "")

        def boom(text, width):
            raise OSError("测量失败")

        box = layout.compute_layout(lines, make_theme(), 460, measure=boom)
        self.assertEqual(box.boxes[0].height, box.line_height)

    def test_measure_returning_garbage_falls_back(self):
        lines = layout.build_lines([{"text": "", "translation": "随便"}], "zh", "", "")
        for bad in (0, -5, None, "x"):
            box = layout.compute_layout(lines, make_theme(), 460, measure=lambda t, w, _b=bad: _b)
            self.assertEqual(box.boxes[0].height, box.line_height, bad)


class BuildLinesTests(unittest.TestCase):
    def test_bilingual_yields_source_then_translation(self):
        lines = layout.build_lines(SEGMENTS, "bilingual", "", "")
        self.assertEqual(
            [(line[0], line[1], line[2]) for line in lines],
            [
                ("おはよう、今日もいい天気だね。", "source", 0),
                ("早上好,今天天气也不错呢。", "translation", 0),
                ("ちょっと待ってて。", "source", 1),
                ("稍等一下。", "translation", 1),
            ],
        )

    def test_zh_only_skips_source(self):
        lines = layout.build_lines(SEGMENTS, "zh", "", "")
        self.assertEqual([line[1] for line in lines], ["translation", "translation"])

    def test_ja_only_skips_translation(self):
        lines = layout.build_lines(SEGMENTS, "ja", "", "")
        self.assertEqual([line[1] for line in lines], ["source", "source"])

    def test_empty_translation_is_skipped(self):
        segments = [{"text": "原文", "translation": ""}]
        lines = layout.build_lines(segments, "bilingual", "", "")
        self.assertEqual([line[1] for line in lines], ["source"])

    def test_user_text_precedes_segments(self):
        lines = layout.build_lines(SEGMENTS, "zh", "你好呀", "")
        self.assertEqual(lines[0], ("你好呀", "user", -1))
        self.assertEqual(lines[1][1], "translation")

    def test_notice_line_is_appended_last(self):
        lines = layout.build_lines(SEGMENTS, "ja", "", "还在回复中")
        self.assertEqual(lines[-1], ("还在回复中", "notice", -1))

    def test_malformed_segments_are_ignored(self):
        lines = layout.build_lines(["nope", {"text": 5}], "bilingual", "", "")
        self.assertEqual(lines, [])


class ComputeLayoutTests(unittest.TestCase):
    def test_line_height_includes_spacing(self):
        layout_box = layout.compute_layout(
            layout.build_lines(SEGMENTS, "ja", "", ""), make_theme(), 460
        )
        self.assertEqual(layout_box.line_height, 30)  # 20 * 1.5

    def test_view_height_is_capped_by_max_lines(self):
        many = [{"text": f"第{i}句", "translation": ""} for i in range(20)]
        layout_box = layout.compute_layout(
            layout.build_lines(many, "ja", "", ""), make_theme(), 460
        )
        self.assertEqual(layout_box.view_height, layout_box.line_height * 4)
        self.assertEqual(layout_box.max_scroll, layout_box.content_height - layout_box.view_height)

    def test_short_content_needs_no_scroll(self):
        layout_box = layout.compute_layout(
            layout.build_lines(SEGMENTS, "ja", "", ""), make_theme(), 460
        )
        self.assertEqual(layout_box.max_scroll, 0)
        self.assertEqual(layout_box.view_height, layout_box.line_height * 2)

    def test_empty_content_keeps_one_line_height(self):
        layout_box = layout.compute_layout([], make_theme(), 460)
        self.assertEqual(layout_box.view_height, layout_box.line_height)
        self.assertEqual(layout_box.boxes, [])

    def test_boxes_are_stacked_in_order(self):
        layout_box = layout.compute_layout(
            layout.build_lines(SEGMENTS, "bilingual", "", ""), make_theme(), 460
        )
        tops = [box.top for box in layout_box.boxes]
        self.assertEqual(tops, [0, 30, 60, 90])

    def test_wrap_width_subtracts_horizontal_padding(self):
        layout_box = layout.compute_layout([], make_theme(), 400)
        self.assertLess(layout_box.wrap_width, 400)

    def test_segment_index_is_carried_to_boxes(self):
        layout_box = layout.compute_layout(
            layout.build_lines(SEGMENTS, "bilingual", "", ""), make_theme(), 460
        )
        self.assertEqual([box.segment for box in layout_box.boxes], [0, 0, 1, 1])


class FollowOffsetTests(unittest.TestCase):
    def test_no_scroll_when_content_fits(self):
        layout_box = layout.compute_layout(
            layout.build_lines(SEGMENTS, "ja", "", ""), make_theme(), 460
        )
        self.assertEqual(layout.follow_offset(layout_box, 1, 0), 0)

    def test_scrolls_down_to_reveal_current_segment(self):
        many = [{"text": f"第{i}句", "translation": ""} for i in range(20)]
        layout_box = layout.compute_layout(
            layout.build_lines(many, "ja", "", ""), make_theme(), 460
        )
        offset = layout.follow_offset(layout_box, 7, 0)
        self.assertGreater(offset, 0)
        self.assertLessEqual(offset, layout_box.max_scroll)
        top = layout_box.boxes[7].top - offset
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + layout_box.line_height, layout_box.view_height)

    def test_offset_is_clamped_to_bounds(self):
        many = [{"text": f"第{i}句", "translation": ""} for i in range(20)]
        layout_box = layout.compute_layout(
            layout.build_lines(many, "ja", "", ""), make_theme(), 460
        )
        self.assertEqual(layout.follow_offset(layout_box, 0, 999), 0)
        self.assertEqual(layout.follow_offset(layout_box, 19, 0), layout_box.max_scroll)


if __name__ == "__main__":
    unittest.main()
