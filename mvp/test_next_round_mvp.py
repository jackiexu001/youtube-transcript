import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

import next_round_mvp as next_round


class NextRoundMvpTests(unittest.TestCase):
    def test_media_child_process_does_not_receive_api_key(self):
        with mock.patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk_secret",
            "OPENAI_API_KEY": "sk-secret",
            "YCA_AI_API_KEY": "secret",
        }, clear=False):
            environment = next_round.media_subprocess_environment()
        self.assertNotIn("GROQ_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("YCA_AI_API_KEY", environment)

    def test_metadata_timeout_has_clear_message(self):
        with mock.patch.object(next_round.base, "require_tool", return_value="yt-dlp"), mock.patch.object(
            next_round.subprocess,
            "run",
            side_effect=next_round.subprocess.TimeoutExpired(["yt-dlp"], 75),
        ):
            with self.assertRaisesRegex(RuntimeError, "尚未下载音频，也未调用 Groq"):
                next_round.audio_info("https://www.youtube.com/watch?v=test")

    def test_short_video_has_no_cut(self):
        self.assertEqual(next_round.adaptive_cut_times(299), [])

    def test_first_cut_is_five_minutes_then_ten_minutes(self):
        self.assertEqual(next_round.adaptive_cut_times(3600), [300, 900, 1500, 2100, 2700, 3300])

    def test_adaptive_first_chunk_is_bounded_and_snapped(self):
        self.assertEqual(next_round.choose_first_chunk_seconds(3600, 2), 120)
        self.assertEqual(next_round.choose_first_chunk_seconds(3600, 15), 180)
        self.assertEqual(next_round.choose_first_chunk_seconds(3600, 100), 300)

    def test_offsets_follow_cut_points(self):
        cuts = [300, 900]
        self.assertEqual(next_round.segment_offset(0, cuts), 0.0)
        self.assertEqual(next_round.segment_offset(1, cuts), 300.0)
        self.assertEqual(next_round.segment_offset(2, cuts), 900.0)

    def test_upload_offsets_include_previous_context(self):
        cuts = [300, 900]
        self.assertEqual(next_round.upload_offset(0, cuts, 10), 0.0)
        self.assertEqual(next_round.upload_offset(1, cuts, 10), 290.0)
        self.assertEqual(next_round.upload_offset(2, cuts, 10), 890.0)

    def test_url_body_does_not_lose_non_ascii_text(self):
        body, boundary = next_round.multipart_fields([("language", "zh"), ("prompt", "中文提示")])
        self.assertIn("中文提示".encode("utf-8"), body)
        self.assertIn(boundary.encode("ascii"), body)

    def test_direct_only_flag(self):
        args = next_round.parse_args(["--direct-only"])
        self.assertTrue(args.direct_only)

    def test_checkpoint_round_trip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = next_round.CaptionCheckpoint(Path(directory) / "resume")
            checkpoint.save(1, 290.0, {"segments": [{"text": "已完成"}]}, 2.5)
            loaded = checkpoint.load()
            self.assertEqual(loaded[1]["offset"], 290.0)
            self.assertEqual(loaded[1]["payload"]["segments"][0]["text"], "已完成")
            checkpoint.cleanup()
            self.assertFalse(checkpoint.directory.exists())

    def test_plan_signature_changes_with_timeline(self):
        first = next_round.plan_signature("https://youtu.be/abc", 1000, [300], 10, "m", "zh", "p")
        second = next_round.plan_signature("https://youtu.be/abc", 1000, [120], 10, "m", "zh", "p")
        self.assertNotEqual(first, second)

    def test_plan_signature_changes_with_prompt(self):
        first = next_round.plan_signature("https://youtu.be/abc", 1000, [300], 10, "m", "zh", "金融")
        second = next_round.plan_signature("https://youtu.be/abc", 1000, [300], 10, "m", "zh", "编程")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
