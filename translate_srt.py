import re

import srt

from translate import (
    _build_usage_summary,
    _print_usage_summary,
    _reset_usage,
    translate_sentences,
)

_SENTENCE_END = re.compile(r"""[.!?…。！？؟♪]['"”’\)\]」』）]*\s*$""")

# Subtitle Jepang/Korea sering tanpa tanda akhir kalimat sama sekali; tanpa
# batas ini seluruh file bisa tergabung jadi satu "kalimat".
MAX_GROUP_CUES = 4


def remove_empty_subtitles(subtitles):
    return [sub for sub in subtitles if sub.content.strip()]


def remove_hearing_impaired(subtitles):
    for sub in subtitles:
        sub.content = re.sub(r"\([^)]*\)", "", sub.content)
        sub.content = re.sub(r"\[[^\]]*\]", "", sub.content)
        sub.content = re.sub(r"（[^）]*）", "", sub.content)
    return subtitles


_ARABIC = re.compile(r"[\u0600-\u06ff]")
_RTL_LINE = re.compile(r"^([.!?…,،:;]*)(.*?)(\s*-)?$")


def fix_rtl_punctuation(subtitles):
    """SRT Arab sering menyimpan tanda baca Latin di AWAL baris dan dash
    dialog di AKHIR (urutan tampilan RTL, bukan urutan logis). Tanpa ini
    akhir kalimat tidak terdeteksi dan cue tergabung lalu terpotong salah."""
    for sub in subtitles:
        lines = []
        for line in sub.content.split("\n"):
            stripped = line.strip()
            if _ARABIC.search(stripped):
                lead, body, dash = _RTL_LINE.match(stripped).groups()
                stripped = ("- " if dash else "") + body.strip() + lead
            lines.append(stripped)
        sub.content = "\n".join(lines)
    return subtitles


def normalize_cue_text(text):
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def group_into_sentences(subtitles):
    groups = []
    current = []
    for sub in subtitles:
        current.append(sub)
        if (
            _SENTENCE_END.search(normalize_cue_text(sub.content))
            or len(current) >= MAX_GROUP_CUES
        ):
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

    # Tanda baca = titik potong sesudahnya. "-" cuma dash dialog (" - "),
    # dipotong SEBELUM dash-nya; "-" di dalam kata ("apa-apa", "Su-an")
    # bukan titik potong.
    anchors = [match.start() + 1 for match in re.finditer(r"\s-\s", target_text)]
    for match in re.finditer(r"[.,!?;:…—]", target_text):
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

    subtitles = fix_rtl_punctuation(subtitles)
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
