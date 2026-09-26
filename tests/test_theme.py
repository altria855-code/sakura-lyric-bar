import math
import unittest

import _path  # noqa: F401  (副作用:把 plugin/ 加入 sys.path)
import theme


class ParseColorTests(unittest.TestCase):
    def test_six_digit_is_opaque(self):
        self.assertEqual(theme.parse_color("#FF8800"), (255, 136, 0, 255))

    def test_eight_digit_uses_leading_alpha(self):
        self.assertEqual(theme.parse_color("#80FF8800"), (255, 136, 0, 128))

    def test_lowercase_and_whitespace_tolerated(self):
        self.assertEqual(theme.parse_color("  #0011aa  "), (0, 17, 170, 255))

    def test_invalid_returns_none(self):
        for bad in ["", "red", "#12345", "#GGGGGG", None, 123, "#1234567"]:
            self.assertIsNone(theme.parse_color(bad), bad)

    def test_signed_and_underscore_digits_are_rejected(self):
        # int(x, 16) 本身接受 "+/-" 与 "_" 分隔符;颜色串必须只由十六进制数字组成
        for bad in ["#+FF880", "#-FF880", "#FF_880", "#F_F880", "# FF880"]:
            self.assertIsNone(theme.parse_color(bad), bad)


class ProjectThemeTests(unittest.TestCase):
    def test_empty_input_yields_full_defaults(self):
        projected = theme.project_theme({})
        self.assertEqual(set(projected), set(theme.DEFAULTS))

    def test_unknown_keys_are_dropped(self):
        projected = theme.project_theme({"nonsense": 1, "fontSize": 30})
        self.assertNotIn("nonsense", projected)
        self.assertEqual(projected["fontSize"], 30)

    def test_out_of_range_numbers_are_clamped(self):
        self.assertEqual(theme.project_theme({"fontSize": 999})["fontSize"], 72)
        self.assertEqual(theme.project_theme({"fontSize": 1})["fontSize"], 10)
        self.assertEqual(theme.project_theme({"backgroundOpacity": -5})["backgroundOpacity"], 0)

    def test_bad_color_falls_back_to_default(self):
        self.assertEqual(
            theme.project_theme({"textColor": "blue"})["textColor"],
            theme.DEFAULTS["textColor"],
        )

    def test_mode_falls_back_when_unknown(self):
        self.assertEqual(theme.project_theme({"mode": "klingon"})["mode"], "bilingual")

    def test_enum_accepts_only_declared_values(self):
        self.assertEqual(theme.project_theme({"syncMode": "timer"})["syncMode"], "timer")
        self.assertEqual(theme.project_theme({"syncMode": "x"})["syncMode"], theme.DEFAULTS["syncMode"])

    def test_booleans_coerce_from_native_bool_only(self):
        self.assertIs(theme.project_theme({"textShadow": True})["textShadow"], True)
        self.assertIs(theme.project_theme({"textShadow": "false"})["textShadow"], theme.DEFAULTS["textShadow"])

    def test_non_mapping_input_is_tolerated(self):
        self.assertEqual(set(theme.project_theme(None)), set(theme.DEFAULTS))

    def test_nan_and_infinity_fall_back_to_default(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(theme.project_theme({"fontSize": bad})["fontSize"], theme.DEFAULTS["fontSize"])
            self.assertEqual(theme.project_theme({"lineSpacing": bad})["lineSpacing"], theme.DEFAULTS["lineSpacing"])

    def test_float_range_is_clamped(self):
        self.assertEqual(theme.project_theme({"lineSpacing": 99})["lineSpacing"], 3.0)
        self.assertEqual(theme.project_theme({"lineSpacing": 0.1})["lineSpacing"], 1.0)

    def test_every_value_is_a_legal_scalar(self):
        projected = theme.project_theme(
            {"fontSize": 10 ** 400, "lineSpacing": float("inf"), "mode": None, "width": float("nan")}
        )
        for key, value in projected.items():
            if isinstance(theme.DEFAULTS[key], bool):
                # 布尔型设置项:bool 是 int 子类,但其合法值本来就是 bool,不参与标量断言
                continue
            self.assertNotIsInstance(value, bool, key)
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), key)
            elif isinstance(value, int):
                self.assertGreaterEqual(value, 0, key)

    def test_huge_integers_are_clamped_not_raised(self):
        # math.isfinite 对超出 double 范围的 int 会抛 OverflowError,不能让它逃逸
        self.assertEqual(theme.project_theme({"fontSize": 10 ** 400})["fontSize"], 72)
        self.assertEqual(theme.project_theme({"fontSize": -(10 ** 400)})["fontSize"], 10)
        self.assertEqual(theme.project_theme({"lineSpacing": 10 ** 400})["lineSpacing"], 3.0)


if __name__ == "__main__":
    unittest.main()
