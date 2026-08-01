# LLM Step Cache Design

## Goal

Persist every validated LLM step so a failed subtitle translation can resume
without repeating successful calls, and identical work can be reused across
different subtitle files.

## Scope

Cache the validated outputs of these LLM operations:

- glossary extraction in `scan_terms()`;
- aligned translation in `translate_chunk()`, including recursively split
  chunks;
- the differently prompted single-sentence fallback in `translate_single()`.

The existing completed-SRT cache remains the fastest path. The step cache is
used only when that cache misses.

## Storage

Reuse the existing SQLite database and cache class. Add an `llm_steps` table:

```sql
CREATE TABLE llm_steps (
    cache_key TEXT PRIMARY KEY,
    step TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

Results are JSON because scan results are dictionaries and translation results
are lists of strings. No TTL or cleanup job is included; cache keys invalidate
old results when relevant inputs change.

## Cache Identity

Build a stable SHA-256 key from canonical JSON containing the step name and all
inputs that can affect its validated result.

For `scan_terms`:

- source chunk;
- model, base URL, temperature, thinking setting;
- scan system prompt and token limits;
- cache version.

For `translate_chunk` and `translate_single`:

- translation variant (`chunk` or `single`);
- source chunk and complete active glossary;
- model, base URL, temperature, thinking setting and output-token limit;
- translation system prompt, request instructions and replacements;
- cache version.

Canonical JSON uses sorted keys and compact separators. Glossary ordering must
not change the key.

## Data Flow

### Glossary scan

1. Compute the scan key and read `llm_steps`.
2. On a hit, return the stored cleaned dictionary.
3. On a miss, perform the current LLM call and adaptive retry.
4. Parse and sanitize the JSON using the current rules.
5. Store only the sanitized dictionary, then return it.

An empty but valid dictionary is cacheable. An empty response, missing JSON, or
invalid JSON is not cacheable.

### Translation

1. Compute the translation key and read `llm_steps`.
2. On a hit, return the stored list of translated strings.
3. On a miss, perform the current LLM call and alignment validation.
4. If aligned, postprocess, store the final list, and return it.
5. If alignment fails, retain the current recursive split behavior. Successful
   child chunks are cached independently; the invalid parent response is not.

Single-sentence fallback follows the same validated translation cache path but
uses a distinct variant in its key because its prompt and output shape differ.

## Concurrency and Failure Handling

Use the existing per-key lock pattern for step keys, with a second cache check
after acquiring the lock. Concurrent identical work then produces one LLM call
within a process.

Cache read/write errors remain non-fatal and fall back to the LLM path. LLM
exceptions and invalid outputs are never written. A later request therefore
retries only missing or failed steps while reusing every completed step.

The existing API error response is unchanged when an uncached step exhausts
all retries.

## Usage Reporting

Step-cache hits make no LLM call and add no token usage. Existing response
fields stay unchanged. Add concise log messages identifying scan or translation
cache hits without logging subtitle content.

## Tests

Add focused tests covering:

1. a successful scan result is reused without another LLM call;
2. a successful translation chunk is reused across different SRT requests;
3. after a later chunk raises, the next request reuses earlier successful
   chunks and calls the LLM only for unfinished work;
4. malformed scan and misaligned translation outputs are not cached;
5. changing relevant prompt/configuration inputs changes the step key;
6. concurrent identical step misses are serialized.

## Deliberately Excluded

- caching raw LLM responses;
- persisting failed responses or retry counters;
- TTL, size limits, eviction, or a cache administration API;
- changing retry/backoff behavior;
- database migration tooling beyond idempotent `CREATE TABLE IF NOT EXISTS`.

These can be added only when operational evidence requires them.
