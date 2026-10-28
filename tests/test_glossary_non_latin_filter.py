import unittest
from unittest.mock import patch

import translate


class GlossaryNonLatinFilterTests(unittest.TestCase):
    """Verifies the anti-CJK-hallucination filter: glossary entries with
    a non-Latin (CJK/Arab/Cyrillic) padanan are dropped so they cannot
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

    def test_scan_terms_keeps_non_latin_source_terms(self):
        # A Japanese/Korean/Arabic source term is legit; only the padanan
        # must be Latin.
        with patch.object(translate, "_chat_with_tokens",
                          return_value='{"佐々木": "Sasaki", "東京": "東京"}'):
            result = translate.scan_terms(["佐々木は東京に行った。"])
        self.assertEqual(result, {"佐々木": "Sasaki"})

    def test_build_glossary_skips_non_latin_from_stale_cache(self):
        # scan_terms already filters, but a poisoned step cache could still
        # return CJK entries; build_glossary must skip them too.
        poisoned = {"Sasaki": "佐佐木", "Itou": "伊藤", "Tokyo": "Tokyo"}
        with patch.object(translate, "scan_terms", return_value=poisoned):
            glossary = translate.build_glossary([["Sasaki went to Tokyo."]])
        self.assertEqual(glossary, {"Tokyo": "Tokyo"})


if __name__ == "__main__":
    unittest.main()