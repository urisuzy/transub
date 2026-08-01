# ASS Subtitle Compatibility Design

## Goal

Add automatic ASS subtitle translation while preserving styling, timing,
positioning, animation, karaoke/control tags, comments, drawings, and all
non-dialogue content. Existing SRT API clients and behavior remain compatible.

## Public API

The existing `/translate` endpoint and RunPod handler keep accepting
`srt_text_base64` and returning `translated_srt_base64`. Despite the legacy
field names, the payload may now contain SRT or ASS.

Detection is content-based:

- ASS when both `[Script Info]` and `[Events]` sections are present,
  allowing an optional UTF-8 BOM and surrounding whitespace;
- SRT otherwise, preserving the current SRT parsing/error behavior.

No explicit format field and no response-shape change are added.

## File Responsibilities

### `translate.py`

Keep all behavior shared by both formats:

- model/configuration and OpenAI client;
- retry and token-usage accounting;
- glossary scanning, chunk translation, postprocessing, and LLM step cache;
- completed-result cache;
- format detection and dispatch;
- RunPod `handler`;
- a `translate_srt()` compatibility wrapper for existing imports.

Imports of format handlers happen locally in the dispatcher so the format
modules can use shared functions without module-import cycles.

### `translate_srt.py`

Move SRT-only behavior out of `translate.py`:

- SRT parsing and composition;
- hearing-impaired and empty-cue filtering;
- cue normalization and sentence grouping;
- translated sentence distribution back to cues;
- SRT-specific uncached pipeline.

The move must preserve existing output and tests.

### `translate_ass.py`

Own ASS-only behavior:

- line-preserving parsing;
- `[Events]` and `Format:` tracking;
- `Dialogue:` field extraction;
- control-token separation and exact reconstruction;
- drawing/comment/malformed-line bypass;
- ASS-specific uncached pipeline.

### `app.py`

Keep the endpoint and schemas unchanged, but call the auto-detect dispatcher
instead of the SRT-only entry point.

## ASS Parsing

Process the input with `splitlines(keepends=True)`. Lines outside translated
Dialogue text are copied verbatim, including line endings.

Within `[Events]`, parse the active `Format:` declaration
case-insensitively and locate the `Text` field. Standard ASS places `Text`
last; only that safe shape is translated because text may contain commas. If
the format is missing, lacks `Text`, or places `Text` before another field,
preserve the affected Dialogue line unchanged and log a concise warning.

For a valid format, split each `Dialogue:` payload at most
`field_count - 1` times. Preserve the original prefix through the final comma
and replace only the Text value.

`Comment:` and every other event type are always copied unchanged.

## Visible Text and Effects

Tokenize each Dialogue Text value into visible segments and immutable ASS
control tokens. Immutable tokens are:

- override blocks from `{` through the matching `}`;
- `\\N` and `\\n` line breaks;
- `\\h` hard spaces.

Control tokens are never sent to the LLM and are reinserted byte-for-byte in
their original sequence.

Join visible segments into one translation unit so inline styling and line
breaks do not destroy sentence context. After translation, divide the result
back across visible segments using their original word/character weights and
word-safe cut points. Reassemble translated segments and original control
tokens.

The split is deterministic: assign translated words to cumulative segment
weight boundaries. When there are fewer translated words than visible
segments, place each available word into the highest-weight remaining segment
and leave the other segments empty. A translation containing no whitespace is
placed wholly in the highest-weight segment. Immutable tokens are retained in
all cases.

This preserves every effect token exactly. Its boundary stays between the same
logical visible segments, although translated wording on either side may have
different lengths.

Dialogue text containing an active ASS drawing mode (`\\p1` through
`\\p9`, until `\\p0`) is copied unchanged. A tag-only or whitespace-only
Dialogue is also copied unchanged.

## Translation Selection

Translate all non-drawing visible `Dialogue:` text regardless of Style,
including dialogue, italics, top-positioned dialogue, titles, and `sign_*`
styles.

Do not apply SRT-specific hearing-impaired removal or cue regrouping to ASS.
Every ASS Dialogue remains one event and retains its Layer, Start, End, Style,
Name, margins, Effect, and ordering.

ASS Dialogue plain text values are passed through the existing glossary and
chunk translation pipeline. Translation step caching can therefore be reused
across subtitle files when visible text and effective glossary/configuration
match.

## Cache Identity

The completed-result cache key includes the detected format and an explicit
handler version: `SRT_HANDLER_VERSION = "1"` or
`ASS_HANDLER_VERSION = "1"`. Existing SRT cache entries must not be confused
with ASS results, and increasing the relevant version invalidates results after
future parser/reconstruction changes.

LLM step-cache keys remain based on effective LLM requests. ASS control tokens
are not part of those requests; different styling around identical visible
text may safely reuse the same translated text and reconstruct their own
effects.

Only completely reconstructed subtitle content is written to the completed
cache. Exceptions never store a partial ASS result.

## Failure Handling

- LLM/API failures retain the current retry behavior and propagate as the
  existing HTTP 500 response after retries are exhausted.
- Invalid ASS structure is not rewritten speculatively: unsafe Dialogue lines
  are copied unchanged.
- A translation count/alignment failure follows the existing recursive split
  and single-sentence fallback.
- If translated text has too few word boundaries, use the deterministic
  highest-weight fallback defined above and retain every immutable token.
- Cache read/write failures remain non-fatal.

## Testing

Add focused tests for:

1. ASS/SRT auto-detection, including BOM/case tolerance;
2. unchanged SRT behavior through the compatibility wrapper;
3. byte-preserved headers, styles, comments, timing, metadata, and newline
   style;
4. inline override blocks and `\\N`/`\\n`/`\\h` preserved exactly;
5. commas inside Dialogue Text;
6. sign styles translated;
7. drawing, tag-only, malformed, and unsafe-Format lines copied unchanged;
8. one ASS event remaining one event in the same order;
9. endpoint/handler round-trip with ASS while retaining legacy field names;
10. completed-cache format isolation;
11. mocked structural processing of local `test/vid.ass` when present,
    without committing that extracted subtitle as a test fixture.

The automated suite uses small synthetic ASS strings. The local
`test/vid.ass` check is supplemental because it is an untracked extracted
media artifact.

## Deliberately Excluded

- adding `pysubs2` or another ASS dependency;
- rewriting styles, fonts, attachments, timing, or event ordering;
- translating comments or vector drawings;
- changing API field names or adding a format parameter;
- perfect linguistic line-breaking typography;
- embedding `test/vid.ass` or `test/vid.mkv` in Git.

These can be revisited only if real files demonstrate that the line-preserving
parser is insufficient.
