# LLM Step Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist validated glossary-scan and translation results so failed subtitle jobs resume from completed LLM work and identical chunks are reused across subtitle files.

**Architecture:** Extend the existing SQLite cache with one JSON-valued `llm_steps` table. `scan_terms()`, `translate_chunk()`, and `translate_single()` compute deterministic keys from effective inputs, double-check the cache under the existing per-key lock, and store only validated outputs. The completed-SRT cache remains the first lookup.

**Tech Stack:** Python standard library (`hashlib`, `json`, `sqlite3`, `unittest`), existing `srt` and OpenAI client dependencies.

## Global Constraints

- Reuse `data/translations.sqlite3`; add no dependency or cache service.
- `TRANSLATION_CACHE_ENABLED=0` disables completed-SRT and step caching.
- Cache only sanitized scan dictionaries and postprocessed, aligned translations.
- Never cache exceptions, malformed scans, or misaligned translations.
- Preserve retry, recursive splitting, API responses, and token-usage behavior.
- No TTL, eviction, administration API, or migration framework.

---

### Task 1: Persist JSON Step Results

**Files:**
- Modify: `translation_cache.py:1-73`
- Test: `tests/test_translation_cache.py`

**Interfaces:**
- Consumes: existing `SQLiteTranslationCache(path)`.
- Produces: `get_step(cache_key: str) -> object | None` and `set_step(cache_key: str, step: str, result: object) -> None`.

- [ ] **Step 1: Write the failing round-trip test**

```python
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
```

- [ ] **Step 2: Verify the test fails**

Run:

```bash
python3 -m unittest tests.test_translation_cache.SQLiteTranslationCacheTests.test_step_round_trip_and_overwrite -v
```

Expected: `ERROR` because `get_step` does not exist.

- [ ] **Step 3: Add the table and JSON methods**

Import `json`. In `_initialize()`, add:

```python
connection.execute(
    """
    CREATE TABLE IF NOT EXISTS llm_steps (
        cache_key TEXT PRIMARY KEY,
        step TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
)
```

Add to `SQLiteTranslationCache`:

```python
def get_step(self, cache_key):
    self._initialize()
    with self._connect() as connection:
        row = connection.execute(
            "SELECT result_json FROM llm_steps WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    return json.loads(row[0]) if row else None

def set_step(self, cache_key, step, result):
    self._initialize()
    result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    with self._connect() as connection:
        connection.execute(
            """
            INSERT INTO llm_steps (cache_key, step, result_json)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                step = excluded.step,
                result_json = excluded.result_json,
                created_at = CURRENT_TIMESTAMP
            """,
            (cache_key, step, result_json),
        )
```

- [ ] **Step 4: Run storage tests**

```bash
python3 -m unittest \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_step_round_trip_and_overwrite \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_round_trip_and_overwrite -v
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add translation_cache.py tests/test_translation_cache.py
git commit -m "feat: persist validated LLM steps"
```

---

### Task 2: Cache Validated Glossary Scans

**Files:**
- Modify: `translate.py:377-424,547-594`
- Test: `tests/test_translation_cache.py`

**Interfaces:**
- Consumes: Task 1 step-cache methods and `translation_key_lock()`.
- Produces: `_step_cache_identity(step: str, payload: dict) -> str`, `_get_cached_step(cache_key: str) -> object | None`, and `_set_cached_step(cache_key: str, step: str, result: object) -> None`.

- [ ] **Step 1: Write failing scan tests**

```python
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
```

- [ ] **Step 2: Verify the tests fail**

```bash
python3 -m unittest \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_scan_terms_reuses_validated_result \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_invalid_scan_is_not_cached \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_step_identity_changes_with_variant_and_payload \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_concurrent_identical_scans_call_llm_once -v
```

Expected: missing identity helper and repeated LLM calls.

- [ ] **Step 3: Add deterministic identity and defensive accessors**

Add near the completed-SRT cache helpers:

```python
def _step_cache_identity(step, payload):
    identity = {
        "version": CACHE_VERSION,
        "step": step,
        "base_url": BASE_URL,
        "model": MODEL,
        "temperature": TEMPERATURE,
        "disable_thinking": DISABLE_THINKING,
        "system_prompt": (
            SCAN_SYSTEM_PROMPT if step == "scan" else SYSTEM_PROMPT
        ),
        "max_tokens": (
            [MAX_TOKENS_SCAN, MAX_TOKENS_SCAN_RETRY]
            if step == "scan"
            else MAX_TOKENS
        ),
        "replacements": replacements if step != "scan" else None,
        "payload": payload,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _get_cached_step(cache_key):
    if not CACHE_ENABLED:
        return None
    try:
        return translation_cache.get_step(cache_key)
    except Exception as exc:
        print(f"LLM step cache read failed: {exc}")
        return None

def _set_cached_step(cache_key, step, result):
    if not CACHE_ENABLED:
        return
    try:
        translation_cache.set_step(cache_key, step, result)
    except Exception as exc:
        print(f"LLM step cache write failed: {exc}")
```

- [ ] **Step 4: Wrap `scan_terms()`**

Construct the existing `user` request first, then compute `_step_cache_identity("scan", {"request": user})`. This fingerprints both the source and exact user instructions. Check the cache before and after acquiring `translation_key_lock(cache_key)`, then run the existing request and validation on a miss. All invalid-response returns stay before the write. After sanitization:

```python
_set_cached_step(cache_key, "scan", cleaned)
return cleaned
```

A parsed `{}` is cacheable; `{}` caused by empty, missing, or malformed JSON is not.

- [ ] **Step 5: Run all current tests**

```bash
python3 -m unittest tests.test_translation_cache -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add translate.py tests/test_translation_cache.py
git commit -m "feat: cache validated glossary scans"
```

---

### Task 3: Cache Translation Chunks and Resume Work

**Files:**
- Modify: `translate.py:367-374,448-499`
- Test: `tests/test_translation_cache.py`

**Interfaces:**
- Consumes: Task 2 helpers.
- Produces: unchanged `translate_single(sentence, glossary=None) -> str` and `translate_chunk(chunk, glossary=None) -> list[str]`, now backed by validated step cache.

- [ ] **Step 1: Write failing reuse and glossary-key tests**

```python
def test_translate_chunk_reuses_validated_result(self):
    with tempfile.TemporaryDirectory() as directory:
        cache = SQLiteTranslationCache(Path(directory) / "cache.sqlite3")
        with (
            patch.object(translate, "translation_cache", cache),
            patch.object(translate, "CACHE_ENABLED", True),
            patch.object(
                translate, "_chat", return_value="1. Halo\\n2. Dunia"
            ) as chat,
        ):
            first = translate.translate_chunk(["Hello", "World"], {})
            second = translate.translate_chunk(["Hello", "World"], {})

        self.assertEqual(first, ["Halo", "Dunia"])
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
                side_effect=["1. Apel\\n2. Pai", "1. Apple\\n2. Pie"],
            ) as chat,
        ):
            translate.translate_chunk(["Apple", "Pie"], {"Apple": "Apel"})
            translate.translate_chunk(["Apple", "Pie"], {"Apple": "Apple"})

        self.assertEqual(chat.call_count, 2)
```

- [ ] **Step 2: Write failing resume and invalid-parent tests**

```python
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
                side_effect=["1. Satu\\n2. Dua", RuntimeError("LLM down")],
            ):
                self.assertEqual(
                    translate.translate_chunk(["One", "Two"], {}),
                    ["Satu", "Dua"],
                )
                with self.assertRaisesRegex(RuntimeError, "LLM down"):
                    translate.translate_chunk(["Three", "Four"], {})

            with patch.object(
                translate, "_chat", return_value="1. Tiga\\n2. Empat"
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
                translate, "_chat", return_value="1. Satu\\n2. Dua"
            ) as second_run:
                self.assertEqual(
                    translate.translate_chunk(["One", "Two"], {}),
                    ["Satu", "Dua"],
                )
                second_run.assert_called_once()
```

- [ ] **Step 3: Verify new tests fail**

```bash
python3 -m unittest \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_translate_chunk_reuses_validated_result \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_translation_key_includes_glossary \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_successful_chunk_survives_later_failure \
  tests.test_translation_cache.SQLiteTranslationCacheTests.test_misaligned_parent_is_not_cached -v
```

Expected: reuse/resume assertions fail.

- [ ] **Step 4: Cache single-sentence translations**

Build the existing single-sentence user request first. Use payload `{"request": user, "glossary": glossary or {}}` and variant `single`; the explicit glossary covers its effect in `postprocess()`. Double-check under the per-key lock, then cache `[translated]` only after `postprocess()` succeeds. A hit returns `cached[0]`.

- [ ] **Step 5: Cache aligned multi-sentence chunks**

For `n > 1`, build the existing numbered user request first, then use payload `{"request": user, "glossary": glossary or {}}` and variant `chunk`. This includes the exact instructions, source lines, and rendered glossary while retaining the glossary explicitly for postprocessing identity. Double-check under the lock. Preserve recursive splitting before any cache write; successful child calls therefore cache their own distinct keys. On alignment success:

```python
translated = [postprocess(r, glossary) for r in results]
_set_cached_step(cache_key, "chunk", translated)
return translated
```

The parent lock may remain held during child calls because every child key differs.

- [ ] **Step 6: Run all tests and static checks**

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile translate.py translation_cache.py app.py
git diff --check
```

Expected: all commands succeed.

- [ ] **Step 7: Commit**

```bash
git add translate.py tests/test_translation_cache.py
git commit -m "feat: resume translations from cached LLM steps"
```

---

### Task 4: Final Regression Verification

**Files:**
- Verify only; no planned changes.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: final evidence for storage, cache reuse, failure recovery, imports, and scoped changes.

- [ ] **Step 1: Run the full suite from a clean process**

```bash
python3 -m unittest discover -s tests -v
```

Expected: every test passes.

- [ ] **Step 2: Verify imports**

```bash
python3 -c "import app, translate, translation_cache; print('imports ok')"
```

Expected: `imports ok`.

- [ ] **Step 3: Inspect the scoped result**

```bash
git status --short
git log -3 --oneline
```

Expected: three implementation commits are present; only known pre-existing user changes remain uncommitted. No SQLite database or secret is tracked.
