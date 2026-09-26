"""设置区块描述符与字体枚举的契约测试(Task 12)。

本文件逐字来自 `.superpowers/sdd/2026-09-25-sakura-lyric-bar/task-12-brief.md`
的测试块,**只有一处**必须偏离,因为测试块内部自相矛盾:

- `test_section_headers_are_status_fields` 要求恰好 3 个 `placement == "section_header"`
  的字段,且它们的 `type` 都是 `status`(宿主也只允许 status 类型用 section_header);
- `test_field_keys_are_unique` 要求全部字段键唯一;
- 而 `test_all_editable_keys_exist_in_theme_defaults` 又要求**所有** status 字段的键
  都取自 `{"overlayStatus", "positionStatus"}` 这 2 个值。

即:5 个 status 字段(3 个分组标题 + 2 个只读状态)× 键唯一 × 键只能取 2 个值 = 无解,
不存在能同时通过这两条断言的实现。因此对分组标题放行(标题不是配置项,不进
`theme.DEFAULTS`,也不受 status 键名约束),其余断言原样保留。
偏离由控制器裁决,详见 task-12-report.md。

评审后按 brief 的 Step 3b 增补了 4 条断言(原文 15 个用例不动):
- `test_all_editable_keys_exist_in_theme_defaults` 补反向集合相等(原为单向 `∈`);
- `test_section_header_keys_and_labels` 覆盖 3 个分组标题的键名、标签与 status 值结构;
- `test_font_options_contain_no_merged_names` / `test_real_enumeration_has_no_merged_names`
  断言字体选项里不得出现 GDI 解析不了的 ` & ` 合并名。
"""

import unittest

import _path  # noqa: F401
import settings_schema as schema

STATUS = {"state": "neutral", "label": "—", "message": ""}


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.descriptor = schema.build_descriptor(["Microsoft YaHei UI", "SimSun"], STATUS, STATUS)

    def test_section_identity(self):
        self.assertEqual(self.descriptor["sectionId"], schema.SECTION_ID)
        self.assertEqual(self.descriptor["title"], "字幕栏")

    def test_field_keys_are_unique(self):
        keys = [field["key"] for field in self.descriptor["fields"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_field_count_within_host_limit(self):
        self.assertLessEqual(len(self.descriptor["fields"]), 32)

    def test_action_count_within_host_limit(self):
        self.assertLessEqual(len(self.descriptor["actions"]), 15)

    def test_section_headers_are_status_fields(self):
        headers = [f for f in self.descriptor["fields"] if f.get("placement") == "section_header"]
        self.assertEqual(len(headers), 3)
        for header in headers:
            self.assertEqual(header["type"], "status")

    def test_status_fields_carry_no_range_attributes(self):
        for field in self.descriptor["fields"]:
            if field["type"] == "status":
                for banned in ("options", "minimum", "maximum", "step", "maxLength", "actionIds"):
                    self.assertNotIn(banned, field, field["key"])

    def test_every_action_has_declared_id(self):
        declared = {action["actionId"] for action in self.descriptor["actions"]}
        self.assertEqual(declared, set(schema.ACTION_IDS))

    def test_danger_is_false_or_absent(self):
        for action in self.descriptor["actions"]:
            self.assertIn(action.get("danger", False), (False,))

    def test_mode_select_lists_three_modes(self):
        field = next(f for f in self.descriptor["fields"] if f["key"] == "mode")
        self.assertEqual([option["value"] for option in field["options"]], ["bilingual", "zh", "ja"])

    def test_font_options_are_capped_at_64(self):
        fonts = [f"字体{i}" for i in range(200)]
        descriptor = schema.build_descriptor(fonts, STATUS, STATUS)
        field = next(f for f in descriptor["fields"] if f["key"] == "fontFamily")
        self.assertEqual(len(field["options"]), 64)

    def test_current_font_is_kept_even_if_not_installed(self):
        fonts = ["微软雅黑"]
        descriptor = schema.build_descriptor(fonts, STATUS, STATUS, current_font="已卸载的字体")
        field = next(f for f in descriptor["fields"] if f["key"] == "fontFamily")
        self.assertIn("已卸载的字体", [option["value"] for option in field["options"]])

    def test_resource_download_action_absent_from_action_list(self):
        action_ids = [action["actionId"] for action in self.descriptor["actions"]]
        self.assertNotIn("install", action_ids)

    def test_all_editable_keys_exist_in_theme_defaults(self):
        import theme

        status_keys = {"overlayStatus", "positionStatus"}
        editable_keys = []
        for field in self.descriptor["fields"]:
            if field.get("placement") == "section_header":
                # 测试块原文没有这一行,但它与另外两条断言互斥(见模块 docstring):
                # 分组标题也是 status 字段,键不可能落在只有 2 个值的 status_keys 里。
                continue
            if field["type"] == "status":
                self.assertIn(field["key"], status_keys)
                continue
            self.assertIn(field["key"], theme.DEFAULTS, field["key"])
            editable_keys.append(field["key"])
        # 反向也要成立:一个可编辑字段都不能漏(原测试块只有单向 ∈,漏字段不会被发现)。
        self.assertEqual(set(editable_keys), set(theme.DEFAULTS))

    def test_section_header_keys_and_labels(self):
        headers = [f for f in self.descriptor["fields"] if f.get("placement") == "section_header"]
        self.assertEqual(
            [header["key"] for header in headers],
            ["headerDisplay", "headerAppearance", "headerBehavior"],
        )
        self.assertEqual(
            [header["label"] for header in headers],
            ["显示", "颜色与材质", "行为"],
        )
        for header in headers:
            # 宿主只认 default,且它会校验结构合法(status 值必须是完整三元组)。
            self.assertEqual(
                set(header["default"]),
                {"state", "label", "message"},
            )
            self.assertEqual(header["default"]["state"], "neutral")
            self.assertEqual(header["default"]["label"], header["label"])
            self.assertEqual(header["default"]["message"], "")

    def test_font_options_contain_no_merged_names(self):
        # 合并名("A & B")在 GDI 里解析不了,会被当成乱填名回落到默认字体 ——
        # 用户选了字体、界面显示已改、画面却不变。选项里必须只有拆开后的单个名字。
        fonts = ["Microsoft YaHei & Microsoft YaHei UI", "SimSun & NSimSun", "MS Gothic & MS UI Gothic"]
        descriptor = schema.build_descriptor(fonts, STATUS, STATUS)
        field = next(f for f in descriptor["fields"] if f["key"] == "fontFamily")
        values = [option["value"] for option in field["options"]]
        for value in values:
            self.assertNotIn(" & ", value)
        for expected in (
            "Microsoft YaHei",
            "Microsoft YaHei UI",
            "SimSun",
            "NSimSun",
            "MS Gothic",
            "MS UI Gothic",
        ):
            self.assertIn(expected, values)


@unittest.skipUnless(__import__("os").name == "nt", "仅 Windows")
class FontEnumerationTests(unittest.TestCase):
    def test_returns_non_empty_unique_names(self):
        fonts = schema.enumerate_fonts(limit=64)
        self.assertTrue(fonts)
        self.assertLessEqual(len(fonts), 64)
        self.assertEqual(len(fonts), len(set(fonts)))

    def test_cjk_fonts_are_prioritised(self):
        fonts = schema.enumerate_fonts(limit=64)
        self.assertTrue(
            any("YaHei" in name or "雅黑" in name or "SimSun" in name or "宋体" in name for name in fonts),
            fonts[:10],
        )

    def test_real_enumeration_has_no_merged_names(self):
        # 本机 64 项里原本有 9 项是 ` & ` 合并名(前 4 名占 3 个),GDI 全部解析不了。
        fonts = schema.enumerate_fonts(limit=64)
        merged = [name for name in fonts if " & " in name]
        self.assertEqual(merged, [], merged)


if __name__ == "__main__":
    unittest.main()
