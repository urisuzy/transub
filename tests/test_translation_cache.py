import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import translate
from translation_cache import SQLiteTranslationCache, translation_key_lock


class SQLiteTranslationCacheTests(unittest.TestCase):
    def test_step_round_trip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")

            self.assertIsNone(cache.get_step("missing"))
            cache.set_step("scan-key", "scan", {"New York": "New York"})
            self.assertEqual(
                cache.get_step("scan-key"),
                {"New York": "New York"},
            )

            cache.set_step("scan-key", "scan", ["hasil terbaru"])
            self.assertEqual(cache.get_step("scan-key"), ["hasil terbaru"])

    def test_round_trip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")

            self.assertIsNone(cache.get("missing"))

            cache.set("key", "source", "model-a", "hasil pertama")
            self.assertEqual(cache.get("key"), "hasil pertama")

            cache.set("key", "source", "model-a", "hasil terbaru")
            self.assertEqual(cache.get("key"), "hasil terbaru")

    def test_identical_keys_are_serialized(self):
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def worker():
            nonlocal active, max_active
            with translation_key_lock("same-key"):
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.01)
                with counter_lock:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(max_active, 1)

    def test_translate_srt_reuses_cached_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            generated = {
                "srt": "1\n00:00:00,000 --> 00:00:01,000\nHalo\n",
                "token_usage": {"total": {"calls": 1}},
            }

            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_translate_srt_uncached",
                    return_value=dict(generated),
                ) as translate_uncached,
            ):
                first = translate.translate_srt("source")
                second = translate.translate_srt("source")

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(second["srt"], generated["srt"])
            self.assertEqual(second["token_usage"]["total"]["calls"], 0)
            translate_uncached.assert_called_once_with("source")

    def test_cache_version_changes_identity(self):
        with patch.object(translate, "CACHE_VERSION", "1"):
            first, _ = translate._translation_cache_identity("source")
        with patch.object(translate, "CACHE_VERSION", "2"):
            second, _ = translate._translation_cache_identity("source")

        self.assertNotEqual(first, second)

    def test_scan_terms_reuses_validated_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_chat_with_tokens",
                    return_value='{"New York": "New York"}',
                ) as chat,
            ):
                first = translate.scan_terms(["Welcome to New York."])
                second = translate.scan_terms(["Welcome to New York."])

            self.assertEqual(first, {"New York": "New York"})
            self.assertEqual(second, first)
            chat.assert_called_once()

    def test_invalid_scan_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_chat_with_tokens",
                    side_effect=["not json", "still not json"] * 2,
                ) as chat,
            ):
                self.assertEqual(translate.scan_terms(["Hello"]), {})
                self.assertEqual(translate.scan_terms(["Hello"]), {})

            self.assertEqual(chat.call_count, 4)

    def test_step_identity_changes_with_variant_and_payload(self):
        scan = translate._step_cache_identity("scan", {"chunk": ["Hello"]})
        changed = translate._step_cache_identity("scan", {"chunk": ["Hi"]})
        single = translate._step_cache_identity("single", {"chunk": ["Hello"]})

        with patch.object(translate, "CACHE_VERSION", "next"):
            next_version = translate._step_cache_identity(
                "scan", {"chunk": ["Hello"]}
            )

        self.assertNotEqual(scan, changed)
        self.assertNotEqual(scan, single)
        self.assertNotEqual(scan, next_version)

    def test_concurrent_identical_scans_call_llm_once(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            barrier = threading.Barrier(4)
            results = []

            def worker():
                barrier.wait()
                results.append(translate.scan_terms(["Hello"]))

            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_chat_with_tokens",
                    return_value='{"Hello": "Halo"}',
                ) as chat,
            ):
                threads = [threading.Thread(target=worker) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(results, [{"Hello": "Halo"}] * 4)
            chat.assert_called_once()

    def test_translate_chunk_reuses_validated_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate, "_chat", return_value="1. Halo\n2. Dunia"
                ) as chat,
            ):
                first = translate.translate_chunk(["Hello", "World"], {})
                second = translate.translate_chunk(["Hello", "World"], {})

            self.assertEqual(first, ["Halo", "Dunia"])
            self.assertEqual(second, first)
            chat.assert_called_once()

    def test_translate_single_reuses_postprocessed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(translate, "_chat", return_value=' "Halo" ') as chat,
            ):
                first = translate.translate_single("Hello", {})
                second = translate.translate_single("Hello", {})

            self.assertEqual(first, "Halo")
            self.assertEqual(second, first)
            chat.assert_called_once()

    def test_translation_key_includes_glossary(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_chat",
                    side_effect=["1. Apel\n2. Pai", "1. Apple\n2. Pie"],
                ) as chat,
            ):
                translate.translate_chunk(["Apple", "Pie"], {"Apple": "Apel"})
                translate.translate_chunk(["Apple", "Pie"], {"Apple": "Apple"})

            self.assertEqual(chat.call_count, 2)

    def test_successful_chunk_survives_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
            ):
                with patch.object(
                    translate,
                    "_chat",
                    side_effect=["1. Satu\n2. Dua", RuntimeError("LLM down")],
                ):
                    self.assertEqual(
                        translate.translate_chunk(["One", "Two"], {}),
                        ["Satu", "Dua"],
                    )
                    with self.assertRaisesRegex(RuntimeError, "LLM down"):
                        translate.translate_chunk(["Three", "Four"], {})

                with patch.object(
                    translate, "_chat", return_value="1. Tiga\n2. Empat"
                ) as resumed:
                    self.assertEqual(
                        translate.translate_chunk(["One", "Two"], {}),
                        ["Satu", "Dua"],
                    )
                    self.assertEqual(
                        translate.translate_chunk(["Three", "Four"], {}),
                        ["Tiga", "Empat"],
                    )
                    resumed.assert_called_once()

    def test_misaligned_parent_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
            ):
                with patch.object(
                    translate,
                    "_chat",
                    side_effect=["invalid", "Satu", "Dua"],
                ) as first_run:
                    self.assertEqual(
                        translate.translate_chunk(["One", "Two"], {}),
                        ["Satu", "Dua"],
                    )
                    self.assertEqual(first_run.call_count, 3)

                with patch.object(
                    translate, "_chat", return_value="1. Satu\n2. Dua"
                ) as second_run:
                    self.assertEqual(
                        translate.translate_chunk(["One", "Two"], {}),
                        ["Satu", "Dua"],
                    )
                    second_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
