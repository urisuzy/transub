import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class SQLiteTranslationCache:
    """Persistent cache for completed SRT translations."""

    def __init__(self, path, busy_timeout_ms=5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self):
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
        )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _initialize(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS translations (
                        cache_key TEXT PRIMARY KEY,
                        source_sha256 TEXT NOT NULL,
                        model TEXT NOT NULL,
                        translated_srt TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            self._initialized = True

    def get(self, cache_key):
        self._initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT translated_srt
                FROM translations
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        return row[0] if row else None

    def set(self, cache_key, source_sha256, model, translated_srt):
        self._initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO translations (
                    cache_key, source_sha256, model, translated_srt
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translated_srt = excluded.translated_srt,
                    created_at = CURRENT_TIMESTAMP
                """,
                (cache_key, source_sha256, model, translated_srt),
            )


_key_locks_guard = threading.Lock()
_key_locks = {}


@contextmanager
def translation_key_lock(cache_key):
    """Serialize identical cache misses within this application process."""

    with _key_locks_guard:
        entry = _key_locks.get(cache_key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _key_locks[cache_key] = entry
        entry["users"] += 1
        lock = entry["lock"]

    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _key_locks_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _key_locks.pop(cache_key, None)
