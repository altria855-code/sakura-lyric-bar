import unittest
from types import SimpleNamespace

import _path  # noqa: F401
import plugin
import view
from test_plugin_runtime_contracts import _Config, _Logger, _Timeline, _Link, _assistant, _segment


class Overlay(_Link):
    def __init__(self):
        super().__init__()
        self.view = view.ViewState()

    def send(self, message):
        super().send(message)
        if message["type"] == "reply":
            self.view.set_reply(message["segments"])
        elif message["type"] == "current":
            self.view.set_current(message["index"])
        return True


class Timeline(_Timeline):
    def __init__(self):
        super().__init__()
        self.history = {}
        self.lookups = []

    def get_entry(self, request):
        self.lookups.append(request["entryId"])
        return {"entry": self.history.get(request["entryId"])}


class PlaybackIdentityTests(unittest.TestCase):
    def setUp(self):
        self.current = {"id": "tian"}
        self.timeline = Timeline()
        self.overlay = Overlay()
        self.runtime = plugin.LyricBarRuntime(
            context=None, plugin_dir="", log=_Logger(), timeline=self.timeline,
            character=SimpleNamespace(current=lambda: dict(self.current)),
            config=_Config(), overlay_link=self.overlay, clock=lambda: 100.0,
        )
        self.addCleanup(self.runtime.stop)
        self.runtime.start()
        self.reply("new", [_segment("new 0"), _segment("new 1"), _segment("new 2")])

    def reply(self, entry_id, segments):
        entry = _assistant(segments, entry_id)
        self.timeline.entries = [entry]
        self.timeline.history[entry_id] = entry
        self.runtime.on_host_event("sakura.host.chat.completed", {"characterId": "tian", "cursor": "latest"})

    def playback(self, playback_id, index=0, entry_id="new", outcome="started", character="tian"):
        name = "sakura.host.tts.started" if outcome == "started" else "sakura.host.tts.ended"
        self.runtime.on_host_event(name, {"playbackId": playback_id, "outcome": outcome,
            "characterId": character, "historyEntryId": entry_id, "segmentIndex": index})

    def test_live_audio_uses_the_playing_segment_including_first_and_duplicate_events(self):
        self.assertEqual(self.overlay.view.current_segment, -1)
        actual = []
        for index in range(3):
            self.playback(f"live-{index}", index)
            self.playback(f"live-{index}", index)
            actual.append(self.overlay.view.current_segment)
        self.assertEqual(actual, [0, 1, 2])

    def test_history_replay_fetches_the_exact_old_entry_and_segment(self):
        self.timeline.history["old"] = _assistant([_segment("old 0"), _segment("old 1")], "old")
        self.playback("history-1", 1, "old")
        self.assertEqual(self.timeline.lookups, ["old"])
        self.assertEqual(self.overlay.view.segments[1]["text"], "old 1")
        self.assertEqual(self.overlay.view.current_segment, 1)
        self.playback("history-2", 0, "old")
        self.assertEqual(self.overlay.view.current_segment, 0)
        self.assertEqual(self.timeline.lookups, ["old"])

    def test_original_segment_index_survives_filtered_blank_segments(self):
        self.reply("sparse", [dict(_segment("silent"), suppressTts=True), _segment(""), _segment("spoken")])
        self.playback("sparse-playback", 2, "sparse")
        self.assertEqual(self.overlay.view.current_segment, 1)
        self.assertEqual(self.overlay.view.segments[1]["text"], "spoken")

    def test_late_end_cannot_clear_a_newer_playback(self):
        self.playback("first", 0)
        self.playback("second", 1)
        self.playback("first", 0, outcome="stopped")
        self.playback("first", 0)  # duplicate started from the retired playback
        self.assertEqual(self.overlay.view.current_segment, 1)
        self.playback("second", 1, outcome="finished")
        self.assertEqual(self.overlay.view.current_segment, -1)

    def test_ended_before_started_cannot_reactivate_the_playback(self):
        self.playback("cancelled", 1, outcome="stopped")
        self.playback("cancelled", 1)
        self.assertEqual(self.overlay.view.current_segment, -1)

    def test_unknown_and_foreign_identity_never_guesses_or_overwrites_current_audio(self):
        self.playback("valid", 1)
        self.playback("foreign", 0, character="other")
        self.runtime.on_host_event("sakura.host.tts.started", {"playbackId": "legacy", "outcome": "started"})
        self.runtime.tick()
        self.assertEqual(self.overlay.view.current_segment, 1)
        self.assertEqual(self.timeline.lookups, [])

    def test_missing_or_foreign_entry_does_not_display_an_unrelated_reply(self):
        self.playback("missing", 0, "not-found")
        self.timeline.history["foreign"] = dict(_assistant([_segment("private")], "foreign"), characterId="other")
        self.playback("foreign-entry", 0, "foreign")
        self.assertEqual(self.overlay.view.segments[0]["text"], "new 0")
        self.assertEqual(self.overlay.view.current_segment, -1)

    def test_character_change_clears_old_subtitles_and_ignores_old_events(self):
        self.playback("old-role", 1)
        self.current["id"] = "other"
        self.runtime.tick()
        self.playback("late-old-role", 2)
        self.runtime.on_host_event("sakura.host.chat.completed", {"characterId": "tian", "cursor": "latest"})
        self.assertEqual(self.overlay.view.segments, [])
        self.assertEqual(self.overlay.view.current_segment, -1)
        self.assertEqual(self.runtime.character_id, "other")

    def test_character_switch_during_history_lookup_discards_the_returned_entry(self):
        def lookup(request):
            self.current["id"] = "other"
            return {"entry": _assistant([_segment("old role")], "old")}
        self.timeline.get_entry = lookup
        self.playback("switching", 0, "old")
        self.assertEqual(self.overlay.view.segments, [])
        self.assertEqual(self.runtime.character_id, "other")

    def test_stop_during_history_lookup_cannot_send_a_late_reply(self):
        def lookup(request):
            self.runtime.stop()
            return {"entry": _assistant([_segment("late")], "old")}
        self.timeline.get_entry = lookup
        before = list(self.overlay.sent)
        self.playback("stopping", 0, "old")
        self.assertEqual(self.overlay.sent, before)

    def test_equal_text_in_a_new_turn_does_not_reuse_the_old_playback(self):
        self.playback("previous", 2)
        self.reply("next", [_segment("new 0"), _segment("new 1"), _segment("new 2")])
        self.assertEqual(self.overlay.view.current_segment, -1)
        self.playback("previous", 2)
        self.assertEqual(self.runtime.entry_id, "next")
        self.assertEqual(self.overlay.view.current_segment, -1)

    def test_busy_conversation_displays_busy_notice(self):
        self.runtime._finish_send({"ok": False, "code": "CHAT_EXECUTION_LIMIT_EXCEEDED"}, "text")
        self.assertEqual(self.overlay.sent[-1], {"type": "notice", "text": "还在回复中"})


if __name__ == "__main__":
    unittest.main()
