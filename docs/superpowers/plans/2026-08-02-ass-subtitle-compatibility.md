# ASS Subtitle Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect and translate ASS subtitles through the existing API while preserving all ASS control tokens and non-text structure, with SRT and ASS handling in separate modules and one shared LLM/core module.

**Architecture:** `translate.py` remains the shared translation core, cache owner, dispatcher, compatibility wrapper, and RunPod entry point. `translate_srt.py` owns SRT parsing/reconstruction; `translate_ass.py` owns line-preserving ASS parsing/reconstruction. The public base64 field names remain unchanged.

**Tech Stack:** Python standard library (`base64`, `dataclasses`, `re`, `unittest`), existing `srt`, FastAPI, OpenAI client, and SQLite cache.

## Global Constraints

- Keep SRT handling in `translate_srt.py` and ASS handling in `translate_ass.py`.
- Keep model configuration, retry, token usage, glossary, batching, postprocessing, and cache logic in `translate.py`.
- Add no dependency; specifically do not add `pysubs2`.
- Preserve `srt_text_base64` and `translated_srt_base64` request/response fields.
- Auto-detect ASS only when `[Script Info]` and `[Events]` are both present; otherwise use SRT.
- Translate visible text from every safe non-drawing `Dialogue:`, including `sign_*` styles.
- Preserve comments, drawings, malformed/unsafe events, headers, styles, timing, event order, control tokens, and line endings.
- Never send ASS control tokens to the LLM.
- Use `SRT_HANDLER_VERSION = "1"` and `ASS_HANDLER_VERSION = "1"` in completed-result cache identity.
- Do not commit `test/vid.ass` or `test/vid.mkv`.

---

### Task 1: Extract SRT Handling Without Behavior Change

**Files:**
- Create: `translate_srt.py`
- Create: `tests/test_translate_srt.py`
- Modify: `translate.py:8,169-297,548-619,718-824`
- Modify: `tests/test_translation_cache.py:64-96`

**Interfaces:**
- Consumes: shared `translate.CHUNK_SIZE`, `translate.CONCURRENCY`, `translate.build_glossary()`, `translate.translate_chunk()`, and usage helpers.
- Produces: `translate_srt.translate_srt_uncached(content: str) -> dict`, `translate.translate_srt(content: str) -> dict`, `translate.translate_sentences(sentences: list[str]) -> list[str]`, and `translate._translate_cached(content, subtitle_format, handler_version, translate_uncached) -> dict`.

- [ ] **Step 1: Write SRT characterization tests**

Create `tests/test_translate_srt.py`:

~~~python
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
~~~

- [ ] **Step 2: Verify the new tests fail**

Run:

~~~bash
python3 -m unittest tests.test_translate_srt -v
~~~

Expected: import error because `translate_srt.py` does not exist.

- [ ] **Step 3: Move SRT-only functions into `translate_srt.py`**

Move `remove_empty_subtitles`, `remove_hearing_impaired`,
`normalize_cue_text`, `group_into_sentences`, `distribute_translation`,
`_find_cut`, and the body of `_translate_srt_uncached`. Use this module
boundary:

~~~python
import re

import srt

from translate import (
    _build_usage_summary,
    _print_usage_summary,
    _reset_usage,
    translate_sentences,
)

_SENTENCE_END = re.compile(r"""[.!?…]['"”’\)\]]*\s*$""")


def remove_empty_subtitles(subtitles):
    return [sub for sub in subtitles if sub.content.strip()]


def remove_hearing_impaired(subtitles):
    for sub in subtitles:
        sub.content = re.sub(r"\([^)]*\)", "", sub.content)
        sub.content = re.sub(r"\[[^\]]*\]", "", sub.content)
    return subtitles


def normalize_cue_text(text):
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def group_into_sentences(subtitles):
    groups = []
    current = []
    for sub in subtitles:
        current.append(sub)
        if _SENTENCE_END.search(normalize_cue_text(sub.content)):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def distribute_translation(translated, group):
    if len(group) == 1:
        return [translated.strip()]

    target_text = translated.strip()
    target_length = len(target_text)
    source_texts = [normalize_cue_text(sub.content) for sub in group]
    cuts = []
    previous = 0
    for cue_index in range(len(group) - 1):
        cut = _find_cut(source_texts, cue_index, target_text, previous)
        cut = max(cut, previous + 1)
        cut = min(cut, target_length - (len(group) - cue_index - 1))
        cuts.append(cut)
        previous = cut

    chunks = []
    previous = 0
    for cut in cuts:
        chunks.append(target_text[previous:cut].strip())
        previous = cut
    chunks.append(target_text[previous:].strip())
    return chunks


def _find_cut(source_texts, cue_index, target_text, previous):
    target_length = len(target_text)
    source = source_texts[cue_index]
    trailing = re.search(r"[.!?…,;:\-—]+$", source)
    if trailing:
        trailing_text = trailing.group()
        position = target_text.find(trailing_text, previous + 1)
        if position != -1:
            cut = position + len(trailing_text)
            while cut < target_length and target_text[cut] == " ":
                cut += 1
            if previous < cut < target_length:
                return cut

    source_lengths = [max(1, len(text)) for text in source_texts]
    target = round(
        target_length
        * sum(source_lengths[:cue_index + 1])
        / sum(source_lengths)
    )
    window = max(8, round(target_length * 0.25))
    lower = max(previous + 1, target - window)
    upper = min(
        target_length - (len(source_texts) - cue_index - 1),
        target + window,
    )

    anchors = []
    for match in re.finditer(r"[.,!?;:…—\-]", target_text):
        position = match.end()
        while position < target_length and target_text[position] == " ":
            position += 1
        anchors.append(position)
    candidates = [position for position in anchors if lower <= position <= upper]
    if candidates:
        return min(candidates, key=lambda position: abs(position - target))

    left = target_text.rfind(" ", lower, target)
    right = target_text.find(" ", target, upper)
    spaces = []
    if left != -1:
        spaces.append((abs(left - target), left + 1))
    if right != -1:
        spaces.append((abs(right - target), right + 1))
    if spaces:
        return min(spaces, key=lambda item: item[0])[1]
    return target


def translate_srt_uncached(srt_content):
    _reset_usage()
    subtitles = list(srt.parse(srt_content))
    print(f"Total subtitles before filter: {len(subtitles)}")

    subtitles = remove_hearing_impaired(subtitles)
    subtitles = remove_empty_subtitles(subtitles)
    print(f"Total subtitles after filter: {len(subtitles)}")

    if not subtitles:
        _print_usage_summary()
        return {"srt": "", "token_usage": _build_usage_summary()}

    groups = group_into_sentences(subtitles)
    sentences = [
        normalize_cue_text(" ".join(sub.content for sub in group))
        for group in groups
    ]
    translated_sentences = translate_sentences(sentences)

    out_subs = []
    for group, translated in zip(groups, translated_sentences):
        pieces = distribute_translation(translated, group)
        for sub, piece in zip(group, pieces):
            sub.content = piece
            out_subs.append(sub)

    for new_index, sub in enumerate(out_subs, start=1):
        sub.index = new_index

    _print_usage_summary()
    return {
        "srt": srt.compose(out_subs),
        "token_usage": _build_usage_summary(),
    }
~~~

Delete the moved SRT functions and `import srt` from `translate.py`.

- [ ] **Step 4: Extract shared sentence orchestration in `translate.py`**

Add one shared reset helper and one shared pipeline used by both formats:

~~~python
def _reset_usage():
    for phase in _TOKEN_USAGE:
        for key in _TOKEN_USAGE[phase]:
            _TOKEN_USAGE[phase][key] = 0


def translate_sentences(sentences):
    if not sentences:
        return []

    chunks = [
        sentences[index:index + CHUNK_SIZE]
        for index in range(0, len(sentences), CHUNK_SIZE)
    ]
    print(f"Pass 1: scanning {len(chunks)} chunks for glossary...")
    glossary = build_glossary(chunks)
    if glossary:
        preview = list(glossary.items())[:5]
        print(
            f"Locked glossary ({len(glossary)} terms): {preview}"
            f"{' ...' if len(glossary) > 5 else ''}"
        )
    else:
        print("Pass 1: no recurring terms detected, proceeding without glossary.")

    print(
        f"Pass 2: translating {len(chunks)} chunks "
        f"(size {CHUNK_SIZE}, concurrency {CONCURRENCY})..."
    )
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        chunk_results = list(
            pool.map(lambda chunk: translate_chunk(chunk, glossary), chunks)
        )
    return [sentence for result in chunk_results for sentence in result]
~~~

Keep only the effective later definition of `_print_usage_summary`; delete the
earlier duplicate definition while moving code.

- [ ] **Step 5: Generalize completed-result caching and keep the wrapper**

Add:

~~~python
SRT_HANDLER_VERSION = "1"
ASS_HANDLER_VERSION = "1"


def _translation_cache_identity(
    content,
    subtitle_format="srt",
    handler_version=SRT_HANDLER_VERSION,
):
    source_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    fingerprint = {
        "version": CACHE_VERSION,
        "subtitle_format": subtitle_format,
        "handler_version": handler_version,
        "base_url": BASE_URL,
        "model": MODEL,
        "chunk_size": CHUNK_SIZE,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "max_tokens_scan": MAX_TOKENS_SCAN,
        "max_tokens_scan_retry": MAX_TOKENS_SCAN_RETRY,
        "disable_thinking": DISABLE_THINKING,
        "system_prompt": SYSTEM_PROMPT,
        "scan_system_prompt": SCAN_SYSTEM_PROMPT,
        "replacements": replacements,
    }
    payload = json.dumps(
        fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    cache_key = hashlib.sha256(
        f"{source_sha256}\n{payload}".encode("utf-8")
    ).hexdigest()
    return cache_key, source_sha256


def _translate_cached(
    content,
    subtitle_format,
    handler_version,
    translate_uncached,
):
    if not CACHE_ENABLED:
        result = translate_uncached(content)
        result["cached"] = False
        return result

    cache_key, source_sha256 = _translation_cache_identity(
        content,
        subtitle_format,
        handler_version,
    )
    cached = _get_cached_translation(cache_key)
    if cached is not None:
        return {
            "srt": cached,
            "token_usage": _empty_usage_summary(),
            "cached": True,
        }

    with translation_key_lock(cache_key):
        cached = _get_cached_translation(cache_key)
        if cached is not None:
            return {
                "srt": cached,
                "token_usage": _empty_usage_summary(),
                "cached": True,
            }
        result = translate_uncached(content)
        _set_cached_translation(cache_key, source_sha256, result["srt"])
        result["cached"] = False
        return result


def translate_srt(srt_content):
    from translate_srt import translate_srt_uncached

    return _translate_cached(
        srt_content,
        "srt",
        SRT_HANDLER_VERSION,
        translate_srt_uncached,
    )
~~~

Add `import translate_srt` to `tests/test_translation_cache.py` and replace
the completed-result wrapper test with:

~~~python
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
~~~

- [ ] **Step 6: Run SRT and cache regression tests**

Run:

~~~bash
python3 -m unittest tests.test_translate_srt tests.test_translation_cache -v
python3 -m py_compile translate.py translate_srt.py
git diff --check
~~~

Expected: all tests pass and static checks exit zero.

- [ ] **Step 7: Commit**

~~~bash
git add translate.py translate_srt.py tests/test_translate_srt.py tests/test_translation_cache.py
git commit -m "refactor: isolate SRT subtitle handling"
~~~

---

### Task 2: Parse and Reconstruct ASS Without an LLM

**Files:**
- Create: `translate_ass.py`
- Create: `tests/test_translate_ass.py`

**Interfaces:**
- Produces: immutable `AssDialogue`, `parse_ass(content: str) -> tuple[list[str], list[AssDialogue]]`, `ass_control_tokens(text: str) -> list[str]`, and `rebuild_ass_text(dialogue: AssDialogue, translated: str) -> str`.
- Does not call the LLM in this task.

- [ ] **Step 1: Write parser and preservation tests**

Create `tests/test_translate_ass.py`:

~~~python
import unittest

from translate_ass import (
    ass_control_tokens,
    parse_ass,
    rebuild_ass_text,
)


ASS_SAMPLE = (
    "\ufeff[Script Info]\r\n"
    "Title: Fixture\r\n"
    "[V4+ Styles]\r\n"
    "Format: Name, Fontname\r\n"
    "Style: main,Arial\r\n"
    "[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Comment: 0,0:00:00.00,0:00:01.00,main,,0,0,0,,Do not translate\r\n"
    "Dialogue: 0,0:00:01.00,0:00:02.00,main,A,0,0,0,,"
    "{\\an8}Hello, world\\Nagain{\\b0}\r\n"
    "Dialogue: 0,0:00:02.00,0:00:03.00,sign_board,,0,0,0,,AUTHORIZED ONLY\r\n"
    "Dialogue: 0,0:00:03.00,0:00:04.00,main,,0,0,0,,"
    "{\\p1}m 0 0 l 10 10{\\p0}\r\n"
)


class AssParserTests(unittest.TestCase):
    def test_parse_preserves_structure_and_extracts_safe_dialogue(self):
        lines, dialogues = parse_ass(ASS_SAMPLE)

        self.assertEqual("".join(lines), ASS_SAMPLE)
        self.assertEqual(len(dialogues), 2)
        self.assertEqual(dialogues[0].source_text, "Hello, world again")
        self.assertEqual(dialogues[1].source_text, "AUTHORIZED ONLY")
        self.assertTrue(dialogues[1].prefix.endswith(","))

    def test_rebuild_preserves_control_tokens_exactly(self):
        _, dialogues = parse_ass(ASS_SAMPLE)
        rebuilt = rebuild_ass_text(
            dialogues[0],
            "Halo dunia lagi",
        )

        self.assertEqual(
            ass_control_tokens(rebuilt),
            [r"{\an8}", r"\N", r"{\b0}"],
        )
        self.assertEqual(
            ass_control_tokens(dialogues[0].original_text),
            ass_control_tokens(rebuilt),
        )
        self.assertEqual(
            rebuilt.replace(r"{\an8}", "")
            .replace(r"\N", " ")
            .replace(r"{\b0}", "")
            .split(),
            ["Halo", "dunia", "lagi"],
        )

    def test_unsafe_format_and_malformed_dialogue_are_not_selected(self):
        unsafe = (
            "[Script Info]\n[Events]\n"
            "Format: Layer, Text, Start\n"
            "Dialogue: 0,hello,0:00:00.00\n"
            "Dialogue: malformed\n"
            "Format: Layer, Text\n"
            "Dialogue: 0,{\\an8 unclosed text\n"
        )
        lines, dialogues = parse_ass(unsafe)

        self.assertEqual("".join(lines), unsafe)
        self.assertEqual(dialogues, [])

    def test_rebuild_preserves_whitespace_around_inline_tags(self):
        inline = (
            "[Script Info]\n[Events]\n"
            "Format: Layer, Text\n"
            "Dialogue: 0,Hello {\\b1}world{\\b0}\n"
        )
        _, dialogues = parse_ass(inline)

        rebuilt = rebuild_ass_text(dialogues[0], "Halo dunia")

        self.assertEqual(rebuilt, r"Halo {\b1}dunia{\b0}")

    def test_tokenizer_preserves_all_supported_ass_escapes(self):
        text = r"{\i1}one\ntwo\hthree\Nfour{\i0}"

        self.assertEqual(
            ass_control_tokens(text),
            [r"{\i1}", r"\n", r"\h", r"\N", r"{\i0}"],
        )


if __name__ == "__main__":
    unittest.main()
~~~

- [ ] **Step 2: Verify tests fail**

~~~bash
python3 -m unittest tests.test_translate_ass.AssParserTests -v
~~~

Expected: import error because `translate_ass.py` does not exist.

- [ ] **Step 3: Implement the line-preserving parser**

Create `translate_ass.py` with these concrete types and helpers:

~~~python
import re
from dataclasses import dataclass

_CONTROL_RE = re.compile(r"(\{[^}]*\}|\\[Nnh])")
_DRAWING_RE = re.compile(r"\\p[1-9]\d*")


@dataclass(frozen=True)
class AssDialogue:
    line_index: int
    prefix: str
    ending: str
    original_text: str
    visible_segments: tuple[str, ...]
    controls: tuple[str, ...]
    source_text: str


def ass_control_tokens(text):
    return _CONTROL_RE.findall(text)


def _split_ending(line):
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _dialogue_from_line(line, line_index, fields):
    if not fields or fields[-1].casefold() != "text":
        return None

    body, ending = _split_ending(line)
    marker, separator, payload = body.partition(":")
    if not separator or marker.strip().casefold() != "dialogue":
        return None

    values = payload.split(",", len(fields) - 1)
    if len(values) != len(fields):
        return None

    original_text = values[-1]
    if _DRAWING_RE.search(original_text):
        return None

    untagged = _CONTROL_RE.sub("", original_text)
    if "{" in untagged or "}" in untagged:
        return None

    parts = _CONTROL_RE.split(original_text)
    visible = tuple(parts[::2])
    controls = tuple(parts[1::2])
    source_parts = []
    for index, segment in enumerate(visible):
        if index:
            control = controls[index - 1]
            if control.casefold() in (r"\n", r"\h"):
                source_parts.append(" ")
        source_parts.append(segment)
    source_text = re.sub(r"\s+", " ", "".join(source_parts)).strip()
    if not source_text:
        return None

    prefix = body[:len(body) - len(original_text)]
    return AssDialogue(
        line_index=line_index,
        prefix=prefix,
        ending=ending,
        original_text=original_text,
        visible_segments=visible,
        controls=controls,
        source_text=source_text,
    )


def parse_ass(content):
    lines = content.splitlines(keepends=True)
    dialogues = []
    in_events = False
    fields = None

    for index, line in enumerate(lines):
        body, _ = _split_ending(line)
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.casefold() == "[events]"
            fields = None
            continue
        if not in_events:
            continue

        marker, separator, payload = body.partition(":")
        kind = marker.strip().casefold() if separator else ""
        if kind == "format":
            fields = [field.strip().casefold() for field in payload.split(",")]
        elif kind == "dialogue":
            dialogue = _dialogue_from_line(line, index, fields)
            if dialogue is not None:
                dialogues.append(dialogue)

    return lines, dialogues
~~~

This intentionally selects signs, skips comments by event type, and skips any
Dialogue containing a nonzero drawing mode.

- [ ] **Step 4: Implement deterministic visible-segment distribution**

Add:

~~~python
def _distribute_words(translated, visible_segments):
    nonempty = [
        (index, max(1, len(segment.strip())))
        for index, segment in enumerate(visible_segments)
        if segment.strip()
    ]
    result = [""] * len(visible_segments)
    words = translated.strip().split()
    if not words or not nonempty:
        return result
    if len(nonempty) == 1:
        result[nonempty[0][0]] = " ".join(words)
        return result
    if len(words) < len(nonempty):
        chosen = sorted(
            sorted(nonempty, key=lambda item: (-item[1], item[0]))[:len(words)]
        )
        for word, (index, _) in zip(words, chosen):
            result[index] = word
        return result

    total_weight = sum(weight for _, weight in nonempty)
    consumed = 0
    word_start = 0
    for position, (index, weight) in enumerate(nonempty[:-1], start=1):
        consumed += weight
        cut = round(len(words) * consumed / total_weight)
        cut = max(word_start + 1, cut)
        cut = min(cut, len(words) - (len(nonempty) - position))
        result[index] = " ".join(words[word_start:cut])
        word_start = cut
    result[nonempty[-1][0]] = " ".join(words[word_start:])
    return result


def rebuild_ass_text(dialogue, translated):
    distributed = _distribute_words(translated, dialogue.visible_segments)
    visible = []
    for original, replacement in zip(dialogue.visible_segments, distributed):
        if not original.strip():
            visible.append(original)
            continue
        leading = original[:len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()):]
        visible.append(leading + replacement + trailing)
    rebuilt = [visible[0]]
    for control, segment in zip(dialogue.controls, visible[1:]):
        rebuilt.extend((control, segment))
    return "".join(rebuilt)
~~~

- [ ] **Step 5: Run parser tests**

~~~bash
python3 -m unittest tests.test_translate_ass.AssParserTests -v
python3 -m py_compile translate_ass.py
git diff --check
~~~

Expected: all tests pass and static checks exit zero.

- [ ] **Step 6: Commit**

~~~bash
git add translate_ass.py tests/test_translate_ass.py
git commit -m "feat: parse ASS subtitles without rewriting effects"
~~~

---

### Task 3: Translate ASS Dialogue Through the Shared Pipeline

**Files:**
- Modify: `translate_ass.py`
- Modify: `tests/test_translate_ass.py`

**Interfaces:**
- Consumes: `translate._reset_usage()`, `translate.translate_sentences()`, `translate._build_usage_summary()`, and `translate._print_usage_summary()`.
- Produces: `translate_ass.translate_ass_uncached(content: str) -> dict`.

- [ ] **Step 1: Write ASS pipeline tests**

Append:

~~~python
from unittest.mock import patch

import translate_ass


class AssPipelineTests(unittest.TestCase):
    def test_pipeline_translates_dialogue_and_sign_but_preserves_effects(self):
        with patch.object(
            translate_ass,
            "translate_sentences",
            return_value=["Halo dunia lagi", "KHUSUS PETUGAS"],
        ) as translate_sentences:
            result = translate_ass.translate_ass_uncached(ASS_SAMPLE)

        output = result["srt"]
        self.assertTrue(output.startswith("\ufeff[Script Info]\r\n"))
        self.assertIn("Style: main,Arial\r\n", output)
        self.assertIn("Comment: 0,0:00:00.00,0:00:01.00,main,,0,0,0,,Do not translate\r\n", output)
        self.assertIn(
            "Dialogue: 0,0:00:01.00,0:00:02.00,main,A,0,0,0,,",
            output,
        )
        self.assertIn(r"{\p1}m 0 0 l 10 10{\p0}", output)
        self.assertIn(r"{\an8}", output)
        self.assertIn(r"\N", output)
        self.assertIn(r"{\b0}", output)
        self.assertIn("KHUSUS PETUGAS", output)
        self.assertEqual(output.count("Dialogue:"), 3)
        self.assertEqual(output.count("\r\n"), ASS_SAMPLE.count("\r\n"))
        translate_sentences.assert_called_once_with(
            ["Hello, world again", "AUTHORIZED ONLY"]
        )

    def test_pipeline_with_no_safe_dialogue_returns_original(self):
        drawing_only = (
            "[Script Info]\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,main,,0,0,0,,{\\p1}m 0 0 l 1 1{\\p0}\n"
        )
        with patch.object(translate_ass, "translate_sentences") as translate_sentences:
            result = translate_ass.translate_ass_uncached(drawing_only)

        self.assertEqual(result["srt"], drawing_only)
        translate_sentences.assert_not_called()
~~~

- [ ] **Step 2: Verify pipeline tests fail**

~~~bash
python3 -m unittest tests.test_translate_ass.AssPipelineTests -v
~~~

Expected: failure because `translate_ass_uncached` does not exist.

- [ ] **Step 3: Implement the ASS uncached pipeline**

Add imports and function:

~~~python
from translate import (
    _build_usage_summary,
    _print_usage_summary,
    _reset_usage,
    translate_sentences,
)


def translate_ass_uncached(content):
    _reset_usage()
    lines, dialogues = parse_ass(content)
    if not dialogues:
        _print_usage_summary()
        return {
            "srt": content,
            "token_usage": _build_usage_summary(),
        }

    translated = translate_sentences(
        [dialogue.source_text for dialogue in dialogues]
    )
    if len(translated) != len(dialogues):
        raise ValueError(
            f"ASS translation count mismatch: "
            f"{len(translated)} != {len(dialogues)}"
        )

    for dialogue, text in zip(dialogues, translated):
        lines[dialogue.line_index] = (
            dialogue.prefix
            + rebuild_ass_text(dialogue, text)
            + dialogue.ending
        )

    _print_usage_summary()
    return {
        "srt": "".join(lines),
        "token_usage": _build_usage_summary(),
    }
~~~

- [ ] **Step 4: Run all ASS and shared-cache tests**

~~~bash
python3 -m unittest tests.test_translate_ass tests.test_translation_cache -v
python3 -m py_compile translate.py translate_srt.py translate_ass.py
git diff --check
~~~

Expected: all tests pass and static checks exit zero.

- [ ] **Step 5: Commit**

~~~bash
git add translate_ass.py tests/test_translate_ass.py
git commit -m "feat: translate ASS dialogue through shared pipeline"
~~~

---

### Task 4: Add Auto-Detection, API Dispatch, and Format-Isolated Cache

**Files:**
- Modify: `translate.py:593-619,718-855`
- Modify: `app.py:1-54`
- Create: `tests/test_subtitle_dispatch.py`
- Modify: `tests/test_translation_cache.py:64-96`

**Interfaces:**
- Consumes: `translate_srt.translate_srt_uncached()` and `translate_ass.translate_ass_uncached()`.
- Produces: `detect_subtitle_format(content: str) -> str` and `translate_subtitle(content: str) -> dict`.
- Keeps `translate_srt()`, `handler(event)`, and API schemas backward-compatible.

- [ ] **Step 1: Write detection, cache-isolation, and dispatch tests**

Create `tests/test_subtitle_dispatch.py`:

~~~python
import base64
import unittest
from unittest.mock import patch

import translate
import translate_ass
import translate_srt


ASS = "\ufeff[script info]\nTitle: x\n[EVENTS]\nFormat: Layer, Text\n"
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


if __name__ == "__main__":
    unittest.main()
~~~

- [ ] **Step 2: Write FastAPI ASS round-trip test**

Append an async test:

~~~python
from app import TranslateRequest
import app as app_module


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
~~~

- [ ] **Step 3: Verify dispatch tests fail**

~~~bash
python3 -m unittest tests.test_subtitle_dispatch -v
~~~

Expected: missing `detect_subtitle_format` and `translate_subtitle`.

- [ ] **Step 4: Implement detection and dispatcher in `translate.py`**

Add:

~~~python
def detect_subtitle_format(content):
    sections = {
        line.strip().lstrip("\ufeff").casefold()
        for line in content.splitlines()
        if line.strip().startswith(("[", "\ufeff["))
    }
    if "[script info]" in sections and "[events]" in sections:
        return "ass"
    return "srt"


def translate_subtitle(content):
    subtitle_format = detect_subtitle_format(content)
    if subtitle_format == "ass":
        from translate_ass import translate_ass_uncached

        return _translate_cached(
            content,
            "ass",
            ASS_HANDLER_VERSION,
            translate_ass_uncached,
        )

    from translate_srt import translate_srt_uncached

    return _translate_cached(
        content,
        "srt",
        SRT_HANDLER_VERSION,
        translate_srt_uncached,
    )
~~~

Keep `translate_srt()` as an explicit SRT wrapper; do not auto-detect inside
that compatibility function.

Replace `handler()` with:

~~~python
def handler(event):
    """RunPod entry point for base64-encoded SRT or ASS content."""
    input_data = event.get("input", {})
    subtitle_text_base64 = input_data.get("srt_text_base64", "")
    if not subtitle_text_base64:
        return {"error": "Base64-encoded subtitle text is required."}

    try:
        subtitle_content = base64.b64decode(
            subtitle_text_base64
        ).decode("utf-8")
    except Exception as exc:
        return {"error": f"Failed to decode base64 subtitle: {exc}"}

    try:
        result = translate_subtitle(subtitle_content)
    except Exception as exc:
        return {"error": f"Translation failed: {exc}"}

    translated_base64 = base64.b64encode(
        result["srt"].encode("utf-8")
    ).decode("utf-8")
    return {
        "translated_srt_base64": translated_base64,
        "token_usage": result["token_usage"],
        "cached": result.get("cached", False),
    }
~~~

- [ ] **Step 5: Route FastAPI through the dispatcher**

Use this import:

~~~python
from translate import translate_subtitle
~~~

~~~python
result = await run_in_threadpool(translate_subtitle, subtitle_content)
~~~

The endpoint body becomes:

~~~python
@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    if not req.srt_text_base64:
        raise HTTPException(
            status_code=400,
            detail="srt_text_base64 is required.",
        )
    try:
        subtitle_content = base64.b64decode(
            req.srt_text_base64
        ).decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to decode base64 subtitle: {exc}",
        )
    try:
        result = await run_in_threadpool(
            translate_subtitle,
            subtitle_content,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Translation failed: {exc}",
        )

    translated_base64 = base64.b64encode(
        result["srt"].encode("utf-8")
    ).decode("utf-8")
    return TranslateResponse(
        translated_srt_base64=translated_base64,
        token_usage=result.get("token_usage"),
        cached=result.get("cached", False),
    )
~~~

The Pydantic request/response field names remain unchanged.

- [ ] **Step 6: Add completed-cache format/version regression**

Keep the wrapper regression from Task 1 and add:

~~~python
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
~~~

- [ ] **Step 7: Run the complete suite and static checks**

~~~bash
python3 -m unittest discover -s tests -v
python3 -m py_compile translate.py translate_srt.py translate_ass.py app.py
python3 -c "import app, translate, translate_srt, translate_ass; print('imports ok')"
git diff --check
~~~

Expected: all tests pass, `imports ok`, and static checks exit zero.

- [ ] **Step 8: Commit**

~~~bash
git add translate.py app.py tests/test_subtitle_dispatch.py tests/test_translation_cache.py
git commit -m "feat: auto-detect ASS subtitle input"
~~~

---

### Task 5: Verify the Real ASS Fixture and Final Regression

**Files:**
- Verify only; do not add `test/vid.ass` or `test/vid.mkv`.

**Interfaces:**
- Consumes: completed Tasks 1–4.
- Produces: evidence that the local fixture retains event/control structure without a network call.

- [ ] **Step 1: Run the full test suite in a clean process**

~~~bash
python3 -m unittest discover -s tests -v
~~~

Expected: every test passes.

- [ ] **Step 2: Run a structural check against `test/vid.ass` when present**

Run:

~~~bash
python3 - <<'PY'
from pathlib import Path
from unittest.mock import patch

import translate_ass

path = Path("test/vid.ass")
if not path.exists():
    print("fixture absent: synthetic tests remain authoritative")
    raise SystemExit(0)

source = path.read_text(encoding="utf-8-sig")
lines, dialogues = translate_ass.parse_ass(source)
translations = [f"TERJEMAHAN {index}" for index in range(len(dialogues))]

with patch.object(
    translate_ass,
    "translate_sentences",
    return_value=translations,
):
    output = translate_ass.translate_ass_uncached(source)["srt"]

output_lines = output.splitlines(keepends=True)
assert len(output_lines) == len(lines)
assert output.count("Dialogue:") == source.count("Dialogue:")
assert output.count("Comment:") == source.count("Comment:")

for dialogue in dialogues:
    old_text = dialogue.original_text
    new_line = output_lines[dialogue.line_index]
    new_text = new_line[len(dialogue.prefix):]
    new_text = new_text[:-len(dialogue.ending)] if dialogue.ending else new_text
    assert translate_ass.ass_control_tokens(old_text) == (
        translate_ass.ass_control_tokens(new_text)
    )

print(f"fixture ok: {len(dialogues)} translated Dialogue events")
PY
~~~

Expected for the current fixture: success with no changed control-token
sequences. The exact translated-event count may be below 299 because drawing,
tag-only, malformed, or unsafe events are intentionally skipped.

- [ ] **Step 3: Verify repository scope and syntax**

~~~bash
python3 -m py_compile translate.py translate_srt.py translate_ass.py app.py
git diff --check
git status --short
~~~

Expected: only planned tracked files plus the pre-existing
`.serena/project.yml`, `test/vid.ass`, and `test/vid.mkv` user artifacts
appear. The media/subtitle fixture files remain untracked.

- [ ] **Step 4: Review commit history**

~~~bash
git log -4 --oneline
~~~

Expected: one scoped commit for each implementation task, with no fixture or
secret committed.
