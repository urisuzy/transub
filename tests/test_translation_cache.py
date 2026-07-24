import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import translate
from translation_cache import SQLiteTranslationCache, translation_key_lock


class SQLiteTranslationCacheTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
