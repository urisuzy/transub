import unittest
from unittest.mock import patch

import translate
import translate_srt as srt_handler


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


if __name__ == "__main__":
    unittest.main()
