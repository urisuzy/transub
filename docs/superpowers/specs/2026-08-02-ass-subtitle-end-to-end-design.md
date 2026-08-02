# ASS Subtitle End-to-End: Carry ASS Through the Pipeline and Preserve Custom Fonts

> **Status:** Approved design
> **Scope:** Multi-repo — auto-leech, transub-api, transub, gduploader.

## Goal

For anime/movie/episode content whose subtitle is ASS, keep it as ASS across the
whole pipeline (do **not** convert to SRT): translate it as ASS, and burn it as
ASS at hardsub. If the source video embeds custom fonts/assets that the ASS
references, those must not break when the subtitle is burned.

## Flow (current)

```
auto-leech (download) -> gduploader/Uploader API (extract sub) -> auto-leech (translate via transub) -> gduploader (encode hardsub)
```

- **auto-leech** (Laravel): gets the ENG subtitle bytes from a remote Uploader
  API, stores a reference in Minio, calls transub to translate, saves the
  translated output. **Both the reference and the translated output are
  hardcoded to `.srt`** regardless of the actual byte format.
- **transub-api** (Flask): receives the subtitle file, base64-encodes,
  forwards to RunPod, returns the translated file. Format-agnostic — ASS passes
  through byte-for-byte.
- **transub** (FastAPI): `translate_subtitle()` auto-detects ASS vs SRT and
  translates ASS → ASS while preserving control tokens, line structure, headers,
  and non-dialogue events. **Already done; no change needed.**
- **gduploader** (Laravel): the ffmpeg worker.
  - Extraction (`AutoSubtitleService::extractIndSubtitle`) forces `-c:s srt`,
    which destroys ASS/SSA → SRT, then `sanitizeSrtForHardsub()` strips ASS tags.
  - Hardsub (`Encoder::buildSubtitleFilter`) uses `ass='/path/x.ass'` for an
    external ASS file (correct), but there is **no font/attachment extraction**,
    so a styled ASS referencing a font embedded in the source video breaks when
    burned from a standalone file.

## Key finding: embedded fonts do NOT auto-load for an external ASS

The font auto-loading only happens on the **embedded-track path**
`subtitles='video.mkv':si=0` (`Encoder.php:252`), where libass reads the
subtitle *from the container* and pulls Matroska attachment fonts for free.

A **translated ASS is a standalone external file**, burned via `ass=`
(`Encoder.php:254`). libass does **not** auto-load the source video's attachment
streams for an external file. Without a `:fontsdir=`, libass falls back to
system fonts → the styled ASS breaks for any non-system font.

Fix: extract the video's attachment streams to a temp fonts dir, then pass
`:fontsdir='/tmp/fonts'` on the `ass=` filter.

## Design: Approach A — format-preserving pass-through everywhere

Treat subtitle content as **opaque bytes with a real extension**; never force
`.srt`. Rejected approaches: B (convert to SRT + `force_style` — loses ASS
layout/effects, fails the goal) and C (custom ASS translator in auto-leech —
duplicates transub's existing `translate_ass.py`; YAGNI).

### 1. auto-leech — stop stamping `.srt`

Core bug is the `.srt` label on bytes that may be ASS. Sniff the bytes once,
then carry the real extension through.

**Format sniff** (shared, format-agnostic — do not trust the remote codec
report, `downloadSoftsub` does not reliably return format):
content contains both `[Script Info]` and `[Events]` (case/BOM-insensitive) →
`.ass`, else `.srt`.

- `app/Services/SubtitleSearchService::translateSubtitle` — save translated
  output to `subtitles/translated_<uuid>.<ext>` instead of hardcoded `.srt`;
  pass the real filename into `TranssubService::translate` so the multipart
  upload carries it (transub-api relays it; transub returns ASS→ASS already).
- `app/Jobs/EpisodeProject/PostEpisode.php`, `app/Jobs/MovieProject/PostMovie.php`,
  `app/Jobs/AnimeProject/PostAnime.php` — same sniff when saving the reference:
  `subtitle-references/<uuid>.<ext>`.

### 2. gduploader extract — keep ASS as ASS

`app/Services/AutoSubtitleService::extractIndSubtitle` currently forces
`-c:s srt` + `sanitizeSrtForHardsub` (strips ASS tags). For `ass`/`ssa` codec:

- Extension: `$ext = in_array($codec, ['ass','ssa']) ? 'ass' : 'srt'`.
- Conversion flag: `$codec === 'subrip' ? '-c copy' : ($ext === 'ass' ? '-c copy' : '-c:s srt')`.
- Temp path: `autosub_<uniqid>_<index>.$ext`.
- Sanitize: skip `sanitizeSrtForHardsub` for ASS (preserves author styling/effects).
- **HI filter: skip entirely for ASS** (decision a). `filterHearingImpaired` is
  SRT-block-based; leave ASS HI lines in rather than write an ASS-aware filter.

`Encoder::buildSubtitleFilter` already emits `ass=` for `.ass` files — no change
needed for the filter selection itself.

### 3. gduploader hardsub — load custom fonts from the video

- **New helper** on `app/Services/Encoder.php`: `extractVideoFonts(videoPath) -> ?string` dumps attachment streams
  to a **per-job temp dir** `fonts_<uniqid>/` via one `-map 0:t` ffmpeg pass.
  Returns the dir path, or `null` when the video has no attachments.
- `app/Services/Encoder.php::buildSubtitleFilter` — when burning an **external
  ASS**, append `:fontsdir='<fontsDir>'` to the `ass=` filter. When no fonts
  were extracted, omit it (libass falls back to system fonts).
- **Cleanup** — the fonts dir is created in the job, handed to the encoder, and
  unlinked in `ProcessEncode::handle`'s existing success + catch blocks alongside
  `$subPath` / `$autoExtractedSubPath`, so it cannot leak on failure.

### 4. transub / transub-api — no change

Verified: transub auto-detects ASS and translates ASS→ASS preserving control
tokens/format; transub-api passes bytes through. Nothing to touch.

## Concurrency & cleanup requirements (explicit)

- **No temp-font collision under parallel encodes:** every fonts dir is
  `fonts_<uniqid>/` — unique per job, so two encodes whose videos embed the same
  font name get separate dirs and never clobber each other. Same `uniqid`
  mechanism the subtitle temp files already use.
- **Reliable temp cleanup:** the fonts dir is unlinked in the **same** success
  + catch paths that already unlink the sub files, scoped to the job's temp
  namespace. A crash mid-encode still removes it.

## Testing

Per-repo, matching existing conventions:
- **transub**: no change; existing ASS tests cover the translation path.
- **auto-leech**: unit test for the format sniff (ass vs srt vs BOM/case);
  assertion that translated + reference paths carry the real extension.
- **gduploader**: unit test that ASS codec extraction keeps `.ass` + `-c copy`
  and skips sanitize/HI; test that `buildSubtitleFilter` emits `:fontsdir=`
  for external ASS with fonts and omits it without; test that the fonts dir is
  removed in both success and catch paths.

## Notes

- No new dependency in any repo.
- `test/vid.ass` / `test/vid.mkv` remain untracked; not committed.
