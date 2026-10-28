import unittest
from datetime import timedelta
from unittest.mock import patch

import translate
import translate_srt as srt_handler
import srt


SRT_SAMPLE = """1
00:00:00,000 --> 00:00:01,000
Hello.

2
00:00:01,000 --> 00:00:02,000
World!
"""


class SrtHandlerTests(unittest.TestCase):
    def test_uncached_pipeline_still_composes_srt(self):
        with patch.object(
            srt_handler,
            "translate_sentences",
            return_value=["Halo.", "Dunia!"],
        ) as translate_sentences:
            result = srt_handler.translate_srt_uncached(SRT_SAMPLE)

        self.assertIn("Halo.", result["srt"])
        self.assertIn("Dunia!", result["srt"])
        self.assertNotIn("Hello.", result["srt"])
        translate_sentences.assert_called_once_with(["Hello.", "World!"])

    def test_public_wrapper_uses_srt_handler(self):
        generated = {
            "srt": "translated",
            "token_usage": {"total": {"calls": 0}},
        }
        with (
            patch.object(translate, "CACHE_ENABLED", False),
            patch.object(
                srt_handler,
                "translate_srt_uncached",
                return_value=generated,
            ) as uncached,
        ):
            result = translate.translate_srt("source")

        self.assertEqual(result["srt"], "translated")
        self.assertFalse(result["cached"])
        uncached.assert_called_once_with("source")


def _cues(*texts):
    return [
        srt.Subtitle(index=i + 1, start=timedelta(seconds=i),
                     end=timedelta(seconds=i + 1), content=text)
        for i, text in enumerate(texts)
    ]


class SentenceGroupingTests(unittest.TestCase):
    def test_cjk_and_arabic_sentence_endings_close_a_group(self):
        groups = srt_handler.group_into_sentences(
            _cues("行こう。", "本当？", "「待って！」", "هل أنت بخير؟", "Hi")
        )
        self.assertEqual([len(g) for g in groups], [1, 1, 1, 1, 1])

    def test_unpunctuated_cues_are_capped_per_group(self):
        groups = srt_handler.group_into_sentences(_cues(*["가자"] * 10))
        self.assertTrue(all(len(g) <= srt_handler.MAX_GROUP_CUES for g in groups))
        self.assertEqual(sum(len(g) for g in groups), 10)

    def test_fullwidth_hearing_impaired_markup_is_removed(self):
        subs = srt_handler.remove_hearing_impaired(_cues("（笑い）行こう"))
        self.assertEqual(subs[0].content, "行こう")


if __name__ == "__main__":
    unittest.main()
