import unittest

import fast_groq_mvp as fast


class FastGroqMvpTests(unittest.TestCase):
    def test_word_timestamps_become_short_cues(self):
        payload = {
            "words": [
                {"word": "你", "start": 0.0, "end": 0.5},
                {"word": "好。", "start": 0.5, "end": 1.5},
                {"word": "Open", "start": 1.5, "end": 2.0},
                {"word": "AI", "start": 2.0, "end": 2.5},
                {"word": "很好!", "start": 2.5, "end": 3.5},
            ]
        }
        cues = fast.fine_segments_from_words(payload, 10.0)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0], {"start": 10.0, "end": 11.5, "text": "你好。"})
        self.assertEqual(cues[1]["text"], "Open AI很好!")

    def test_single_letter_fragment_is_joined(self):
        self.assertEqual(fast.join_recognized_words(["T", "erry。"]), "Terry。")

    def test_exact_segment_text_is_preserved(self):
        text = "OpenAI offers a frontend role, and the team uses React. 第二句话用来测试切分。"
        payload = {
            "segments": [{"text": text, "start": 0.0, "end": 8.0}],
            "words": [
                {"word": "Open", "start": 0.0, "end": 0.5},
                {"word": "AI", "start": 0.5, "end": 0.8},
                {"word": "off", "start": 0.8, "end": 1.1},
                {"word": "ers", "start": 1.1, "end": 1.4},
                {"word": "React.", "start": 3.0, "end": 3.5},
                {"word": "第二句话用来测试切分。", "start": 3.5, "end": 8.0},
            ],
        }
        cues = fast.fine_segments_from_payload(payload, 0.0)
        joined = "".join(cue["text"] for cue in cues)
        self.assertEqual(joined.replace(" ", ""), text.replace(" ", ""))
        self.assertNotIn("off ers", joined)

    def test_merge_removes_cross_chunk_overlap(self):
        chunks = [
            [
                {"start": 590.0, "end": 594.0, "text": "左一"},
                {"start": 594.0, "end": 598.0, "text": "左二"},
            ],
            [
                {"start": 590.0, "end": 594.0, "text": "右一"},
                {"start": 594.0, "end": 598.0, "text": "右二"},
                {"start": 598.0, "end": 602.0, "text": "后段"},
            ],
        ]
        merged = fast.merge_fine_segments(chunks, [0.0, 590.0], 10)
        self.assertEqual([item["text"] for item in merged], ["左一", "右二", "后段"])
        self.assertTrue(
            all(merged[index]["end"] <= merged[index + 1]["start"] for index in range(len(merged) - 1))
        )

    def test_merge_repairs_overlapping_timestamps(self):
        chunks = [[
            {"start": 0.0, "end": 3.0, "text": "第一段"},
            {"start": 2.5, "end": 4.0, "text": "第二段"},
        ]]
        merged = fast.merge_fine_segments(chunks, [0.0], 10)
        self.assertEqual(merged[0]["end"], merged[1]["start"])


if __name__ == "__main__":
    unittest.main()
