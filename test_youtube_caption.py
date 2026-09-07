import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import youtube_caption as archive


class NetworkReliabilityTests(unittest.TestCase):
    def setUp(self):
        archive.EVENT_FORMAT = "text"
        archive.ACTIVE_PROXY = None

    def test_chinese_channel_is_normalized(self):
        value = archive.normalize_channel_url("https://www.youtube.com/@Ruby投资笔记")
        self.assertTrue(value.endswith("/@Ruby%E6%8A%95%E8%B5%84%E7%AC%94%E8%AE%B0/videos"))

    def test_invalid_channel_has_actionable_message(self):
        with self.assertRaisesRegex(archive.ArchiveRequestError, "完整的 YouTube"):
            archive.normalize_channel_url("https://example.com/channel")

    def test_error_classification(self):
        self.assertEqual(archive.classify_request_error("HTTP Error 429: Too Many Requests")[0], "rate_limited")
        self.assertEqual(archive.classify_request_error("read operation timed out")[0], "timeout")
        self.assertEqual(archive.classify_request_error("Sign in to confirm you’re not a bot")[0], "verification_required")

    def test_proxy_password_is_redacted(self):
        self.assertNotIn("secret", archive.redact_secrets("https://name:secret@127.0.0.1:8080"))

    def test_groq_key_is_redacted(self):
        secret = "gsk_" + "test-only-not-a-real-secret-123456"
        self.assertNotIn(secret, archive.redact_secrets("request failed for " + secret))

    def test_openai_key_is_redacted(self):
        secret = "sk-" + "test-only-not-a-real-secret-123456"
        self.assertNotIn(secret, archive.redact_secrets("request failed for " + secret))

    def test_ai_service_outage_pauses_channel(self):
        upstream = type("GroqFailure", (Exception,), {"status_code": 503})("service unavailable")
        error = archive.normalized_ai_error(upstream)
        self.assertEqual(error.code, "ai_connection_error")
        self.assertTrue(archive.should_stop_channel(error, 0))

    def test_existing_archive_error_is_preserved(self):
        original = archive.ArchiveRequestError("ai_component_missing", "缺少组件", retryable=False)
        self.assertIs(archive.normalized_ai_error(original), original)

    def test_metadata_timeout_retries_then_stops(self):
        timeout = subprocess.TimeoutExpired(["yt-dlp"], archive.YTDLP_TOTAL_TIMEOUT_SECONDS)
        with mock.patch.object(archive.subprocess, "run", side_effect=timeout) as run, mock.patch.object(
            archive.time, "sleep"
        ):
            with self.assertRaises(archive.ArchiveRequestError) as raised:
                archive.run_json(["--dump-single-json", "https://www.youtube.com/watch?v=test"])
        self.assertEqual(raised.exception.code, "timeout")
        self.assertEqual(run.call_count, archive.YTDLP_ATTEMPTS)

    def test_bot_check_uses_public_client_fallback(self):
        blocked = subprocess.CompletedProcess(
            ["yt-dlp"], 1, stdout="", stderr="Sign in to confirm you’re not a bot"
        )
        recovered = subprocess.CompletedProcess(
            ["yt-dlp"], 0, stdout='{"id": "rUhllpnYWR8"}', stderr=""
        )
        with mock.patch.object(archive.subprocess, "run", side_effect=[blocked, recovered]) as run:
            result = archive.run_json(["--dump-single-json", "https://youtu.be/rUhllpnYWR8"])
        self.assertEqual(result["id"], "rUhllpnYWR8")
        self.assertEqual(run.call_count, 2)
        fallback_command = run.call_args_list[1].args[0]
        self.assertIn("--extractor-args", fallback_command)
        self.assertIn("youtube:player_client=android_vr,web_embedded,tv_simply", fallback_command)

    def test_retry_queue_round_trip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            queue = {}
            error = archive.ArchiveRequestError("timeout", "连接超时", "detail", True)
            archive.queue_video_retry(queue, "abc", "测试视频", error)
            archive.save_retry_queue(folder, queue)
            loaded = archive.load_retry_queue(folder)
            self.assertEqual(loaded["abc"]["error_type"], "timeout")
            self.assertEqual(loaded["abc"]["attempts"], 1)
            archive.save_retry_queue(folder, {})
            self.assertFalse(archive.retry_queue_path(folder).exists())

    def test_rate_limit_immediately_pauses_channel(self):
        error = archive.ArchiveRequestError("rate_limited", "限流", retryable=False)
        self.assertTrue(archive.should_stop_channel(error, 1))

    def test_three_consecutive_network_errors_pause_channel(self):
        error = archive.ArchiveRequestError("connection_error", "断网", retryable=True)
        self.assertFalse(archive.should_stop_channel(error, 2))
        self.assertTrue(archive.should_stop_channel(error, 3))

    def test_rate_limit_saves_queue_and_does_not_request_next_video(self):
        playlist = {
            "channel": "测试频道",
            "entries": [
                {"id": "first", "title": "第一条"},
                {"id": "second", "title": "第二条"},
            ],
        }
        limited = archive.ArchiveRequestError("rate_limited", "YouTube 暂时限流", retryable=False)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            archive, "run_json", side_effect=[playlist, limited]
        ) as run:
            archive.process_channel(
                Path(directory),
                {"url": "https://www.youtube.com/@test", "delay_seconds": 0},
            )
            queue = archive.load_retry_queue(Path(directory) / "测试频道")
        self.assertEqual(run.call_count, 2)
        self.assertIn("first", queue)
        self.assertNotIn("second", queue)

    def test_success_removes_old_retry_item(self):
        playlist = {"channel": "测试频道", "entries": [{"id": "first", "title": "第一条"}]}
        info = {
            "id": "first",
            "title": "第一条",
            "upload_date": "20260906",
            "subtitles": {},
            "automatic_captions": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "测试频道"
            folder.mkdir()
            archive.save_retry_queue(folder, {"first": {"id": "first", "attempts": 1}})
            with mock.patch.object(archive, "run_json", side_effect=[playlist, info]):
                archive.process_channel(
                    root,
                    {"url": "https://www.youtube.com/@test", "delay_seconds": 0},
                )
            self.assertFalse(archive.retry_queue_path(folder).exists())
            self.assertTrue((folder / "2026-09-06_first.html").exists())

    def test_ai_captions_replace_no_subtitle_page(self):
        playlist = {"channel": "测试频道", "entries": [{"id": "first", "title": "第一条"}]}
        info = {
            "id": "first",
            "title": "第一条",
            "upload_date": "20260906",
            "subtitles": {},
            "automatic_captions": {},
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            archive, "run_json", side_effect=[playlist, info]
        ), mock.patch.object(
            archive,
            "transcribe_missing_video",
            return_value=([(0.0, "AI 生成的第一句"), (4.2, "AI 生成的第二句")], {"model": "test"}),
        ) as transcribe:
            root = Path(directory)
            archive.process_channel(
                root,
                {"url": "https://www.youtube.com/@test", "delay_seconds": 0},
                ai_transcribe_missing=True,
                groq_api_key="gsk_" + "test-only-not-a-real-secret-123456",
            )
            page = (root / "测试频道" / "2026-09-06_first.html").read_text(encoding="utf-8")
        transcribe.assert_called_once()
        self.assertIn("AI 生成的第一句", page)
        self.assertIn("Groq AI 字幕", page)
        self.assertIn('class="line" role="button"', page)
        self.assertIn('data-start="4.2"', page)
        self.assertIn("enablejsapi:1", page)
        self.assertIn("commandPlayer('seekTo'", page)

    def test_old_caption_page_is_upgraded_for_full_row_click(self):
        old_page = '''<html><head><style>.line{display:grid}</style></head><body>
<section class="transcript"><div class="line"><button class="time" onclick="seek(4)">00:04</button><div>旧字幕</div></div></section>
<script>function seek(s){}</script></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            page_path = folder / "old.html"
            page_path.write_text(old_page, encoding="utf-8")
            changed = archive.upgrade_caption_page_interactions(folder)
            upgraded = page_path.read_text(encoding="utf-8")
        self.assertEqual(changed, 1)
        self.assertIn('role="button"', upgraded)
        self.assertIn('data-start="4"', upgraded)
        self.assertIn("captionKey(event,4,this)", upgraded)
        self.assertIn("function captionKey(", upgraded)


if __name__ == "__main__":
    unittest.main()
