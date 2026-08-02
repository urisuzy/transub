import re
from dataclasses import dataclass

from translate import (
    _build_usage_summary,
    _print_usage_summary,
    _reset_usage,
    translate_sentences,
)

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
