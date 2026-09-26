import unittest

import _path  # noqa: F401
import timeline_source as ts

ASSISTANT = {
    "entryId": "e2",
    "kind": "assistant",
    "origin": "chat",
    "createdAt": "2026-09-25T10:00:03+08:00",
    "payload": {"segments": [{"text": "おはよう", "translation": "早上好"}]},
}
HUMAN = {
    "entryId": "e1",
    "kind": "human",
    "origin": "chat",
    "createdAt": "2026-09-25T10:00:00+08:00",
    "payload": {"text": "你好"},
}


class DisplayTests(unittest.TestCase):
    def test_assistant_segments_are_collected(self):
        result = ts.display_from_entries([ASSISTANT], "tian", [], 0.0)
        self.assertEqual(result["segments"], [{"text": "おはよう", "translation": "早上好"}])
        self.assertTrue(result["changed"])

    def test_last_assistant_entry_wins(self):
        second = dict(ASSISTANT, entryId="e3", payload={"segments": [{"text": "第二", "translation": "第二"}]})
        result = ts.display_from_entries([ASSISTANT, second], "tian", [], 0.0)
        self.assertEqual(result["segments"][0]["text"], "第二")

    def test_human_entry_becomes_user_text(self):
        result = ts.display_from_entries([HUMAN, ASSISTANT], "tian", [], 0.0)
        self.assertEqual(result["user_text"], "你好")

    def test_locally_sent_text_is_deduped(self):
        result = ts.display_from_entries([HUMAN, ASSISTANT], "tian", [("你好", 100.0)], 102.0)
        self.assertEqual(result["user_text"], "")

    def test_dedupe_clears_even_when_an_older_human_exists(self):
        # 一批里既有旧的、又有刚本地发过的:去重命中必须**清空**该轴,
        # 不能把旧值留下(否则陈旧文本会经 changed=True 推上屏,覆盖刚发的话)。
        older = dict(HUMAN, payload={"text": "更早说的话"})
        result = ts.display_from_entries(
            [older, HUMAN, ASSISTANT], "tian", [("你好", 100.0)], 102.0
        )
        self.assertEqual(result["user_text"], "")

    def test_entries_with_current_character_id_are_used(self):
        # 真实宿主条目**必定**带 characterId(等于当前角色)。这条覆盖主干路径 ——
        # 少了它,"凡带 characterId 的条目一律丢弃"这种错误实现也能全绿。
        owned = dict(ASSISTANT, characterId="tian")
        result = ts.display_from_entries([owned], "tian", [], 0.0)
        self.assertEqual(result["segments"], [{"text": "おはよう", "translation": "早上好"}])

    def test_owned_human_entry_shows_text(self):
        owned = dict(HUMAN, characterId="tian")
        result = ts.display_from_entries([owned, dict(ASSISTANT, characterId="tian")], "tian", [], 0.0)
        self.assertEqual(result["user_text"], "你好")

    def test_user_text_axis_marks_change(self):
        # changed 的 user_text 支路:桌面端直接输入的文本变化时要报 True
        first = ts.display_from_entries([HUMAN], "tian", [], 0.0)
        second = ts.display_from_entries(
            [dict(HUMAN, payload={"text": "换了一句"})], "tian", [], 0.0,
            previous_segments=first["segments"], previous_user_text=first["user_text"],
        )
        self.assertTrue(second["changed"])

    def test_segments_with_text_and_translation_both_empty_are_dropped(self):
        weird = dict(ASSISTANT, payload={"segments": [{"text": "", "translation": ""}]})
        result = ts.display_from_entries([weird], "tian", [], 0.0)
        self.assertEqual(result["segments"], [])

    def test_dedupe_expires(self):
        result = ts.display_from_entries([HUMAN, ASSISTANT], "tian", [("你好", 100.0)], 106.0)
        self.assertEqual(result["user_text"], "你好")

    def test_other_character_entries_are_ignored(self):
        foreign = dict(ASSISTANT, characterId="someone-else")
        entries = [foreign]
        result = ts.display_from_entries(entries, "tian", [], 0.0)
        self.assertEqual(result["segments"], [])

    def test_no_assistant_entry_keeps_segments_empty(self):
        result = ts.display_from_entries([HUMAN], "tian", [], 0.0)
        self.assertEqual(result["segments"], [])

    def test_malformed_entries_are_tolerated(self):
        result = ts.display_from_entries(["nope", None, {"kind": "assistant"}], "tian", [], 0.0)
        self.assertEqual(result["segments"], [])

    def test_no_change_reports_false(self):
        # `changed` 是**纯函数式**的:由调用方传入的上一轮结果比较得出,
        # 而不是函数自己记状态 —— 所以必须把上一轮的 segments/user_text 传回来。
        first = ts.display_from_entries([ASSISTANT], "tian", [], 0.0)
        second = ts.display_from_entries(
            [ASSISTANT], "tian", [], 0.0,
            previous_segments=first["segments"], previous_user_text=first["user_text"],
        )
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])

    def test_changed_reports_true_when_reply_differs(self):
        first = ts.display_from_entries([ASSISTANT], "tian", [], 0.0)
        other = dict(ASSISTANT, payload={"segments": [{"text": "别的话", "translation": ""}]})
        second = ts.display_from_entries(
            [other], "tian", [], 0.0,
            previous_segments=first["segments"], previous_user_text=first["user_text"],
        )
        self.assertTrue(second["changed"])

    def test_no_human_entry_yields_empty_user_text(self):
        # 一批里没有 human 条目时,该轴**为空** —— 不回填上一轮的文本。
        # 「用户那行保持显示」由调用方负责:收到空文本就不推 user 消息,
        # 浮窗自然保持原样;而回填会在去重命中时把陈旧文本顶回屏幕
        # (见 test_dedupe_clears_even_when_an_older_human_exists 的反例)。
        result = ts.display_from_entries(
            [ASSISTANT], "tian", [], 0.0,
            previous_segments=[], previous_user_text="我先前说的话",
        )
        self.assertEqual(result["user_text"], "")

    def test_segments_without_text_are_filtered(self):
        weird = dict(ASSISTANT, payload={"segments": [{"translation": "只有译文"}, "x"]})
        result = ts.display_from_entries([weird], "tian", [], 0.0)
        self.assertEqual(result["segments"], [{"text": "", "translation": "只有译文"}])


if __name__ == "__main__":
    unittest.main()
