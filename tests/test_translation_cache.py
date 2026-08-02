import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import translate
import translate_srt
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
                "srt": "translated",
                "token_usage": {"total": {"calls": 1}},
            }
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate_srt,
                    "translate_srt_uncached",
                    return_value=dict(generated),
                ) as uncached,
            ):
                first = translate.translate_srt("source")
                second = translate.translate_srt("source")

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(second["srt"], "translated")
            self.assertEqual(second["token_usage"]["total"]["calls"], 0)
            uncached.assert_called_once_with("source")

    def test_cache_version_changes_identity(self):
        with patch.object(translate, "CACHE_VERSION", "1"):
            first, _ = translate._translation_cache_identity("source")
        with patch.object(translate, "CACHE_VERSION", "2"):
            second, _ = translate._translation_cache_identity("source")

        self.assertNotEqual(first, second)

    def test_handler_versions_change_completed_cache_identity(self):
        srt_key, _ = translate._translation_cache_identity(
            "source", "srt", "1"
        )
        ass_key, _ = translate._translation_cache_identity(
            "source", "ass", "1"
        )
        ass_v2_key, _ = translate._translation_cache_identity(
            "source", "ass", "2"
        )

        self.assertNotEqual(srt_key, ass_key)
        self.assertNotEqual(ass_key, ass_v2_key)

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

    def test_parsed_empty_scan_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate, "_chat_with_tokens", return_value="{}"
                ) as chat,
            ):
                self.assertEqual(translate.scan_terms(["Hello"]), {})
                self.assertEqual(translate.scan_terms(["Hello"]), {})

            chat.assert_called_once()

    def test_poisoned_scan_rows_are_misses_and_overwritten(self):
        for poisoned in (["not", "a", "dict"], {" Term ": " Value "}):
            with (
                self.subTest(poisoned=poisoned),
                tempfile.TemporaryDirectory() as directory,
            ):
                cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
                cache.set_step("scan-key", "scan", poisoned)
                with (
                    patch.object(translate, "translation_cache", cache),
                    patch.object(translate, "CACHE_ENABLED", True),
                    patch.object(
                        translate, "_step_cache_identity", return_value="scan-key"
                    ),
                    patch.object(
                        translate,
                        "_chat_with_tokens",
                        return_value='{"Hello": "Halo"}',
                    ) as chat,
                ):
                    expected = {"Hello": "Halo"}
                    self.assertEqual(translate.scan_terms(["Hello"]), expected)
                    self.assertEqual(translate.scan_terms(["Hello"]), expected)

                chat.assert_called_once()
                self.assertEqual(cache.get_step("scan-key"), {"Hello": "Halo"})

    def test_disabled_step_cache_bypasses_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            cache.set_step("scan-key", "scan", {"Old": "Lama"})
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", False),
                patch.object(
                    translate, "_step_cache_identity", return_value="scan-key"
                ),
                patch.object(
                    translate,
                    "_chat_with_tokens",
                    return_value='{"New": "Baru"}',
                ) as chat,
            ):
                self.assertEqual(translate.scan_terms(["Hello"]), {"New": "Baru"})
                self.assertEqual(translate.scan_terms(["Hello"]), {"New": "Baru"})

            self.assertEqual(chat.call_count, 2)
            self.assertEqual(cache.get_step("scan-key"), {"Old": "Lama"})

    def test_cached_scan_preserves_first_writer_glossary_order(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate,
                    "_chat_with_tokens",
                    return_value='{"term": "first", "TERM": "second"}',
                ) as chat,
            ):
                first = translate.build_glossary([["Hello"]])
                second = translate.build_glossary([["Hello"]])

            self.assertEqual(first, {"term": "first"})
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

    def test_poisoned_single_rows_are_misses_and_overwritten(self):
        for poisoned in ("not a list", [], ["one", "two"], [1]):
            with (
                self.subTest(poisoned=poisoned),
                tempfile.TemporaryDirectory() as directory,
            ):
                cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
                cache.set_step("single-key", "single", poisoned)
                with (
                    patch.object(translate, "translation_cache", cache),
                    patch.object(translate, "CACHE_ENABLED", True),
                    patch.object(
                        translate, "_step_cache_identity", return_value="single-key"
                    ),
                    patch.object(translate, "_chat", return_value="Halo") as chat,
                ):
                    self.assertEqual(translate.translate_single("Hello"), "Halo")
                    self.assertEqual(translate.translate_single("Hello"), "Halo")

                chat.assert_called_once()
                self.assertEqual(cache.get_step("single-key"), ["Halo"])

    def test_poisoned_chunk_rows_are_misses_and_overwritten(self):
        poisoned_rows = (
            "not a list",
            ["only one"],
            ["one", 2],
            ["one", "two", "three"],
        )
        for poisoned in poisoned_rows:
            with (
                self.subTest(poisoned=poisoned),
                tempfile.TemporaryDirectory() as directory,
            ):
                cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
                cache.set_step("chunk-key", "chunk", poisoned)
                with (
                    patch.object(translate, "translation_cache", cache),
                    patch.object(translate, "CACHE_ENABLED", True),
                    patch.object(
                        translate, "_step_cache_identity", return_value="chunk-key"
                    ),
                    patch.object(
                        translate, "_chat", return_value="1. Halo\n2. Dunia"
                    ) as chat,
                ):
                    expected = ["Halo", "Dunia"]
                    self.assertEqual(
                        translate.translate_chunk(["Hello", "World"]), expected
                    )
                    self.assertEqual(
                        translate.translate_chunk(["Hello", "World"]), expected
                    )

                chat.assert_called_once()
                self.assertEqual(cache.get_step("chunk-key"), expected)

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

    def test_equivalent_glossary_orders_reuse_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate, "_chat", return_value="1. Halo\n2. Dunia"
                ) as chat,
            ):
                first = translate.translate_chunk(
                    ["Hello", "World"], {"World": "Dunia", "Hello": "Halo"}
                )
                second = translate.translate_chunk(
                    ["Hello", "World"], {"Hello": "Halo", "World": "Dunia"}
                )

            self.assertEqual(second, first)
            chat.assert_called_once()

    def test_step_cache_hit_logs_kind_and_key_without_subtitle_content(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
            cache.set_step("scan-key-123456", "scan", {"Term": "Istilah"})
            cache.set_step(
                "chunk-key-123456", "chunk", ["Terjemahan 1", "Terjemahan 2"]
            )

            def identity(step, _payload):
                return f"{step}-key-123456"

            output = io.StringIO()
            with (
                patch.object(translate, "translation_cache", cache),
                patch.object(translate, "CACHE_ENABLED", True),
                patch.object(
                    translate, "_step_cache_identity", side_effect=identity
                ),
                patch.object(translate, "_chat", side_effect=AssertionError),
                patch.object(
                    translate, "_chat_with_tokens", side_effect=AssertionError
                ),
                redirect_stdout(output),
            ):
                translate.scan_terms(["SECRET SCAN CUE"])
                translate.translate_chunk(
                    ["SECRET CHUNK CUE 1", "SECRET CHUNK CUE 2"]
                )

            log = output.getvalue()
            self.assertIn("Scan cache hit: scan-key-123", log)
            self.assertIn("Translation cache hit: chunk-key-12", log)
            self.assertNotIn("SECRET", log)

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
