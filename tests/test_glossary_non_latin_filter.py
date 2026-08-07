import unittest
from unittest.mock import patch

import translate


class GlossaryNonLatinFilterTests(unittest.TestCase):
    """Verifies the anti-CJK-hallucination filter: glossary entries with
    non-Latin (CJK/Arab/Cyrillic) keys or values are dropped so they cannot
    anchor a Pass 2 chunk into a foreign language."""

    def test_has_non_latin_detects_cjk(self):
        self.assertTrue(translate._has_non_latin("佐佐木"))
        self.assertTrue(translate._has_non_latin("好，知道了。"))
        self.assertTrue(translate._has_non_latin("한글"))

    def test_has_non_latin_detects_arabic_and_cyrillic(self):
        self.assertTrue(translate._has_non_latin("مرحبا"))
        self.assertTrue(translate._has_non_latin("Привет"))

    def test_has_non_latin_allows_latin_and_extended(self):
        self.assertFalse(translate._has_non_latin("Sasaki"))
        self.assertFalse(translate._has_non_latin("café"))
        self.assertFalse(translate._has_non_latin("Señor — naïve"))
        self.assertFalse(translate._has_non_latin("Ångström"))

    def test_scan_terms_drops_non_latin_entries(self):
        # Model hallucinates a CJK padanan for a Japanese name.
        with patch.object(translate, "_chat_with_tokens",
                          return_value='{"Sasaki": "佐佐木", "Tokyo": "Tokyo"}'):
            result = translate.scan_terms(["Sasaki went to Tokyo."])
        self.assertEqual(result, {"Tokyo": "Tokyo"})
        self.assertNotIn("Sasaki", result)

    def test_build_glossary_skips_non_latin_from_stale_cache(self):
        # scan_terms already filters, but a poisoned step cache could still
        # return CJK entries; build_glossary must skip them too.
        poisoned = {"Sasaki": "佐佐木", "Itou": "伊藤", "Tokyo": "Tokyo"}
        with patch.object(translate, "scan_terms", return_value=poisoned):
            glossary = translate.build_glossary([["Sasaki went to Tokyo."]])
        self.assertEqual(glossary, {"Tokyo": "Tokyo"})


if __name__ == "__main__":
    unittest.main()