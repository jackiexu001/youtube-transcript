import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cloud_asr_mvp as mvp


class CloudAsrMvpTests(unittest.TestCase):
    def test_video_key(self):
        self.assertEqual(mvp.video_key("https://www.youtube.com/watch?v=jpoMabs9t4s"), "jpoMabs9t4s")
        self.assertEqual(mvp.video_key("https://youtu.be/jpoMabs9t4s?t=20"), "jpoMabs9t4s")

    def test_supported_provider_settings(self):
        self.assertEqual(mvp.provider_config("groq")["model"], "whisper-large-v3-turbo")
        self.assertEqual(mvp.provider_config("openai")["model"], "whisper-1")
        self.assertTrue(mvp.provider_config("openai")["api_url"].startswith("https://api.openai.com/"))

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            mvp.provider_config("unknown")

    def test_openai_compatible_request_uses_selected_endpoint_and_model(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"segments":[{"start":0,"end":1,"text":"ok"}]}'

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "speech.m4a"
            audio.write_bytes(b"audio")
            config = mvp.provider_config("openai")
            with mock.patch.object(mvp.urllib.request, "urlopen", return_value=Response()) as opened:
                payload = mvp.transcribe_audio(
                    audio, "sk-test-key-1234567890", config["api_url"], config["model"], "zh", "", "OpenAI"
                )
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertIn(b"whisper-1", request.data)
        self.assertEqual(payload["segments"][0]["text"], "ok")

    def test_normalized_segments_adds_offset(self):
        payload = {"segments": [{"start": 1.25, "end": 2.5, "text": " 你好 "}]}
        self.assertEqual(
            mvp.normalized_segments(payload, 590.0),
            [{"start": 591.25, "end": 592.5, "text": "你好"}],
        )

    def test_overlap_midpoint_removes_duplicate_region(self):
        left = [
            {"start": 0.0, "end": 590.0, "text": "前段"},
            {"start": 592.0, "end": 596.0, "text": "左重叠"},
        ]
        right = [
            {"start": 590.0, "end": 594.0, "text": "右重叠"},
            {"start": 596.0, "end": 600.0, "text": "后段"},
        ]
        merged = mvp.merge_by_overlap_midpoint([left, right], [0.0, 590.0], 600, 10)
        self.assertEqual([item["text"] for item in merged], ["前段", "左重叠", "后段"])

    def test_writes_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            segments = [{"start": 1.0, "end": 2.25, "text": "测试"}]
            mvp.write_outputs(output, segments, {"ok": True})
            self.assertIn("00:00:01,000 --> 00:00:02,250", (output / "groq_transcript.srt").read_text())
            self.assertTrue(json.loads((output / "groq_report.json").read_text())["ok"])


if __name__ == "__main__":
    unittest.main()
