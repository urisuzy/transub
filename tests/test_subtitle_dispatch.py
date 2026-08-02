import base64
import unittest
from unittest.mock import patch

import translate
import translate_ass
import translate_srt

from app import TranslateRequest
import app as app_module


ASS = "﻿[script info]\nTitle: x\n[EVENTS]\nFormat: Layer, Text\n"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


class SubtitleDispatchTests(unittest.TestCase):
    def test_detects_ass_case_insensitively_with_bom(self):
        self.assertEqual(translate.detect_subtitle_format(ASS), "ass")
        self.assertEqual(translate.detect_subtitle_format(SRT), "srt")

    def test_cache_identity_isolated_by_format_and_handler_version(self):
        srt_key, _ = translate._translation_cache_identity(
            "same", "srt", "1"
        )
        ass_key, _ = translate._translation_cache_identity(
            "same", "ass", "1"
        )
        ass_v2_key, _ = translate._translation_cache_identity(
            "same", "ass", "2"
        )

        self.assertNotEqual(srt_key, ass_key)
        self.assertNotEqual(ass_key, ass_v2_key)

    def test_dispatches_ass_and_srt_to_separate_modules(self):
        ass_result = {"srt": "ass-out", "token_usage": {}}
        srt_result = {"srt": "srt-out", "token_usage": {}}
        with (
            patch.object(translate, "CACHE_ENABLED", False),
            patch.object(
                translate_ass,
                "translate_ass_uncached",
                return_value=ass_result,
            ) as ass_handler,
            patch.object(
                translate_srt,
                "translate_srt_uncached",
                return_value=srt_result,
            ) as srt_handler,
        ):
            self.assertEqual(
                translate.translate_subtitle(ASS)["srt"],
                "ass-out",
            )
            self.assertEqual(
                translate.translate_subtitle(SRT)["srt"],
                "srt-out",
            )

        ass_handler.assert_called_once_with(ASS)
        srt_handler.assert_called_once_with(SRT)

    def test_runpod_handler_keeps_legacy_fields_for_ass(self):
        encoded = base64.b64encode(ASS.encode()).decode()
        with patch.object(
            translate,
            "translate_subtitle",
            return_value={
                "srt": "translated-ass",
                "token_usage": {},
                "cached": False,
            },
        ):
            result = translate.handler(
                {"input": {"srt_text_base64": encoded}}
            )

        self.assertEqual(
            base64.b64decode(result["translated_srt_base64"]).decode(),
            "translated-ass",
        )
        self.assertIn("cached", result)


class FastApiDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_keeps_legacy_fields_for_ass(self):
        encoded = base64.b64encode(ASS.encode()).decode()
        with patch.object(
            app_module,
            "translate_subtitle",
            return_value={
                "srt": "translated-ass",
                "token_usage": {"total": {"calls": 1}},
                "cached": False,
            },
        ) as dispatcher:
            response = await app_module.translate(
                TranslateRequest(srt_text_base64=encoded)
            )

        self.assertEqual(
            base64.b64decode(response.translated_srt_base64).decode(),
            "translated-ass",
        )
        dispatcher.assert_called_once_with(ASS)


if __name__ == "__main__":
    unittest.main()
