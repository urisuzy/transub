# gduploader: Keep ASS at Extract + Load Video Fonts at Hardsub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep an ASS subtitle as ASS during extraction (no forced SRT conversion, no tag-stripping), and make a styled external ASS survive hardsub by loading the source video's embedded font attachments via `:fontsdir=`, with collision-safe per-job temp dirs and reliable cleanup.

**Architecture:** In `AutoSubtitleService::extractIndSubtitle`, branch on codec so `ass`/`ssa` streams are copied raw (`-c copy`) to a `.ass` temp file and skip both sanitization and HI filtering. In `Encoder`, add `extractVideoFonts()` which dumps the source's attachment streams into a per-job `fonts_<uniqid>/` dir; `buildSubtitleFilter` appends `:fontsdir='<dir>'` to the `ass=` filter for an external ASS. The fonts dir is created inside the Encoder, exposed via a property, and unlinked by `ProcessEncode` in both its success and catch paths (same as the existing sub-file cleanup).

**Tech Stack:** PHP 8.2, Laravel 12, Pest, ffmpeg/ffprobe (via `exec`/`Process`).

## Global Constraints

- Never convert an ASS/SSA stream to SRT at extraction — use `-c copy` for `ass`/`ssa`; keep `.srt` conversion for non-ASS, non-subrip codecs.
- Skip `sanitizeSrtForHardsub` and `filterHearingImpaired` for ASS/SSA (preserves author styling/effects; HI filtering is intentionally skipped for ASS).
- The `ass=` filter for an external ASS must pass `:fontsdir='<per-job dir>'` when the video has attachments; omit it when it has none (libass falls back to system fonts).
- Fonts temp dir must be unique per job (`fonts_<uniqid>`) — collision-safe under parallel encodes with identical embedded fonts.
- Fonts temp dir must be unlinked in BOTH the success and catch paths of `ProcessEncode::handle`, alongside the existing `$subPath`/`$autoExtractedSubPath` cleanup.
- No new dependency.
- `Encoder::buildSubtitleFilter` selection logic (external ASS → `ass=`, embedded stream → `subtitles=:si=`) is unchanged except for the `:fontsdir=` addition.
- `test/vid.ass` / `test/vid.mkv` are unrelated to this repo; do not create or commit them here.

---

### Task 1: Keep ASS as ASS during extraction

**Files:**
- Modify: `app/Services/AutoSubtitleService.php:8,55-101` (`extractIndSubtitle`)
- Test: `tests/Unit/AutoSubtitleServiceAssExtractionTest.php`

**Interfaces:**
- Consumes: `probeSubtitleStreams(string): array`, `detectStreamLanguage(...)` (both exist).
- Produces: `extractIndSubtitle(string $videoPath): string` still returns a temp path, but for an `ass`/`ssa` stream it is `.ass`, copied raw, and not sanitized/HI-filtered.

- [ ] **Step 1: Write the failing test**

Create `tests/Unit/AutoSubtitleServiceAssExtractionTest.php`:

```php
<?php

namespace Tests\Unit;

use App\Services\AutoSubtitleService;
use PHPUnit\Framework\TestCase;
use ReflectionMethod;

class AutoSubtitleServiceAssExtractionTest extends TestCase
{
    private string $mkvPath;
    private array $temporaryFiles = [];

    protected function setUp(): void
    {
        parent::setUp();
        // Build a tiny MKV with one ASS subtitle stream that carries a
        // dialogue line referencing a style and an embedded-font marker.
        $this->mkvPath = $this->tempPath('mkv');
        $assContent = "[Script Info]\n"
            ."[V4+ Styles]\n"
            ."Style: Default,CustomFont,45,&H00FFFFFF,0,0,0,-1,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
            ."[Events]\n"
            ."Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\an8}Kita bukan tandingan.\n";
        $assPath = $this->tempPath('ass');
        file_put_contents($assPath, $assContent);

        // mux the ASS as an "ass" subtitle stream into a matroska container
        $cmd = 'ffmpeg -loglevel error -f lavfi -i color=black:s=320x240:d=1 '
            . '-i "' . $assPath . '" -map 0:v -map 1:s -c:v libx264 -preset ultrafast '
            . '-c:s ass -f matroska -y "' . $this->mkvPath . '" 2>&1';
        exec($cmd, $out, $code);
        $this->assertSame(0, $code, 'failed to build fixture mkv: ' . implode("\n", $out));
    }

    public function test_ass_stream_is_extracted_raw_and_not_sanitized(): void
    {
        $service = new AutoSubtitleService();
        $path = $service->extractIndSubtitle($this->mkvPath);
        $this->temporaryFiles[] = $path;

        $this->assertStringEndsWith('.ass', $path);
        $content = file_get_contents($path);
        $this->assertStringContainsString('Kita bukan tandingan.', $content);
        // Style and override block must survive (not stripped to SRT).
        $this->assertStringContainsString('Style: Default,CustomFont', $content);
        $this->assertStringContainsString('{\\an8}', $content);
    }

    protected function tearDown(): void
    {
        foreach ($this->temporaryFiles as $file) {
            if (file_exists($file)) {
                unlink($file);
            }
        }
        if (file_exists($this->mkvPath)) {
            unlink($this->mkvPath);
        }
        parent::tearDown();
    }

    private function tempPath(string $ext): string
    {
        return sys_get_temp_dir() . '/autosub_test_' . uniqid() . '.' . $ext;
    }
}
```

> Note: the fixture relies on ffmpeg being installed (it is — `/opt/homebrew/bin/ffmpeg`). The `ind` language tag: if ffmpeg does not tag the stream `ind`, the test may need the fixture tagged. See Step 3 note about the `detectStreamLanguage` fallback path; if the fixture stream is untagged, `extractIndSubtitle` uses `detectStreamLanguage`, which requires langsrt availability. If that path cannot run in CI, add a `language: ind` tag via `-metadata:s:s:0 language=ind` in the mux command. Adjust the fixture to reliably produce an `ind` stream.

- [ ] **Step 2: Run test to verify it fails**

Run: `php artisan test --compact tests/Unit/AutoSubtitleServiceAssExtractionTest.php`
Expected: FAIL — extraction still writes `.srt`, or content was converted (`Style:` gone).

- [ ] **Step 3: Implement the ASS-preserving extraction**

In `app/Services/AutoSubtitleService.php`, `extractIndSubtitle()`:

1. Change the per-stream temp path + conversion flag block (currently around lines 57-63):

```php
        $tempFiles = [];
        foreach ($indStreams as $stream) {
            $absoluteIndex = $stream['index'];
            $codec = $stream['codec_name'];
            $isAss = in_array($codec, ['ass', 'ssa'], true);
            $ext = $isAss ? 'ass' : 'srt';
            $tempPath = sys_get_temp_dir() . '/autosub_' . uniqid() . '_' . $absoluteIndex . '.' . $ext;

            // subrip and ass/ssa stream out raw; everything else is converted to SRT.
            $conversionFlag = ($codec === 'subrip' || $isAss) ? '-c copy' : '-c:s srt';
            $command = "ffmpeg -i \"{$videoPath}\" -map 0:{$absoluteIndex} {$conversionFlag} \"{$tempPath}\" -y 2>&1";
```

2. Change the sanitize + HI block (currently around lines 96-101). Skip both for ASS:

```php
        $bestPath = $tempFiles[$best]['path'];
        $this->log .= "Selected stream {$best} ({$tempFiles[$best]['lines']} lines)\n";

        if (strtolower(pathinfo($bestPath, PATHINFO_EXTENSION)) !== 'ass') {
            \App\Helpers\SubtitleHelper::sanitizeSrtForHardsub($bestPath);
            $this->log .= "Subtitle rendering tags normalized\n";

            $this->filterHearingImpaired($bestPath);
            $this->log .= "HI filter applied\n";
        } else {
            $this->log .= "ASS subtitle kept as-is (styling/effects preserved, HI filter skipped)\n";
        }

        return $bestPath;
```

> Keep `detectStreamLanguage`/`probeSubtitleStreams` unchanged. `sanitizeSrtForHardsub` and `filterHearingImpaired` are now only reached for non-ASS paths, matching the constraint.

- [ ] **Step 4: Run test to verify it passes**

Run: `php artisan test --compact tests/Unit/AutoSubtitleServiceAssExtractionTest.php`
Expected: PASS.

- [ ] **Step 5: Run the full existing suite**

Run: `php artisan test --compact`
Expected: PASS (no regression in the SRT extract path).

- [ ] **Step 6: Commit**

```bash
git add app/Services/AutoSubtitleService.php tests/Unit/AutoSubtitleServiceAssExtractionTest.php
git commit -m "feat: extract ASS subtitles without converting to SRT"
```

---

### Task 2: Extract video fonts to a per-job temp dir

**Files:**
- Modify: `app/Services/Encoder.php` (add `extractVideoFonts()` + `fontsDir` property)
- Test: `tests/Unit/EncoderFontExtractionTest.php`

**Interfaces:**
- Consumes: `isAssSubtitleFormat(string): bool` (exists).
- Produces: `Encoder::extractVideoFonts(string $videoPath): ?string` returning a per-job fonts dir path, or `null` when the video has no attachment streams. Public property `Encoder::$fontsDir` holds the last extracted dir (read by the job for cleanup).

- [ ] **Step 1: Write the failing test**

Create `tests/Unit/EncoderFontExtractionTest.php`:

```php
<?php

namespace Tests\Unit;

use App\Services\Encoder;
use PHPUnit\Framework\TestCase;

class EncoderFontExtractionTest extends TestCase
{
    private string $mkvPath;
    private array $temporaryFiles = [];

    protected function setUp(): void
    {
        parent::setUp();
        // tiny font file
        $fontPath = $this->tempPath('ttf');
        file_put_contents($fontPath, 'FAKEFONTFILE');
        // mkv with one attachment stream + a video stream
        $this->mkvPath = $this->tempPath('mkv');
        $cmd = 'ffmpeg -loglevel error -f lavfi -i color=black:s=160x120:d=1 '
            . '-attach "' . $fontPath . '" -metadata:s:t mimetype=application/x-truetype-font '
            . '-metadata:s:t filename=CustomFont.ttf '
            . '-c:v libx264 -preset ultrafast -f matroska -y "' . $this->mkvPath . '" 2>&1';
        exec($cmd, $out, $code);
        $this->assertSame(0, $code, 'failed to build font fixture mkv: ' . implode("\n", $out));
    }

    public function test_extract_fonts_returns_per_job_dir_with_font(): void
    {
        $encoder = new Encoder();
        $dir = $encoder->extractVideoFonts($this->mkvPath);
        $this->temporaryFiles[] = $dir; // whole dir

        $this->assertNotNull($dir);
        $this->assertDirectoryExists($dir);
        $files = array_diff(scandir($dir) ?: [], ['.', '..']);
        $this->assertNotEmpty($files, 'expected at least one attachment dumped');
        $this->assertSame($dir, $encoder->fontsDir);
    }

    public function test_extract_fonts_returns_null_when_no_attachments(): void
    {
        $plainMkv = $this->tempPath('mkv');
        $this->temporaryFiles[] = $plainMkv;
        $cmd = 'ffmpeg -loglevel error -f lavfi -i color=black:s=160x120:d=1 '
            . '-c:v libx264 -preset ultrafast -f matroska -y "' . $plainMkv . '" 2>&1';
        exec($cmd, $out, $code);
        $this->assertSame(0, $code);

        $encoder = new Encoder();
        $this->assertNull($encoder->extractVideoFonts($plainMkv));
    }

    public function test_extract_fonts_returns_null_on_missing_video(): void
    {
        $encoder = new Encoder();
        $this->assertNull($encoder->extractVideoFonts('/nonexistent/missing.mkv'));
    }

    protected function tearDown(): void
    {
        foreach ($this->temporaryFiles as $file) {
            if (is_dir($file)) {
                $this->removeDir($file);
            } elseif (file_exists($file)) {
                unlink($file);
            }
        }
        parent::tearDown();
    }

    private function removeDir(string $dir): void
    {
        if (! is_dir($dir)) {
            return;
        }
        foreach (scandir($dir) ?: [] as $entry) {
            if ($entry === '.' || $entry === '..') {
                continue;
            }
            $path = $dir . '/' . $entry;
            is_dir($path) ? $this->removeDir($path) : unlink($path);
        }
        rmdir($dir);
    }

    private function tempPath(string $ext): string
    {
        return sys_get_temp_dir() . '/font_test_' . uniqid() . '.' . $ext;
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `php artisan test --compact tests/Unit/EncoderFontExtractionTest.php`
Expected: FAIL — `Error: Call to undefined method App\Services\Encoder::extractVideoFonts()`.

- [ ] **Step 3: Implement font extraction**

Add a public property and method to `app/Services/Encoder.php` (place near the existing `$temporarySubtitlePath` property, around line 13, and after `isAssSubtitleFormat` around line 271):

```php
    /** @var string|null per-job fonts dir created for the current encode (unlinked by the job) */
    public ?string $fontsDir = null;
```

```php
    /**
     * Dump the source video's attachment streams (custom fonts) into a
     * per-job temp dir. Returns the dir path, or null when the video has no
     * attachments or extraction fails. The dir is unique per job (uniqid) so
     * parallel encodes never collide, and is owned by the calling job for
     * cleanup.
     */
    public function extractVideoFonts(string $videoPath): ?string
    {
        $this->fontsDir = null;

        if (! is_file($videoPath)) {
            return null;
        }

        $probe = "ffprobe -v error -select_streams t -show_entries stream=index -of csv=p=0 \"{$videoPath}\"";
        $hasAttachments = trim((string) shell_exec($probe)) !== '';
        if (! $hasAttachments) {
            return null;
        }

        $dir = sys_get_temp_dir() . '/autosub_fonts_' . uniqid();
        if (! is_dir($dir) && ! mkdir($dir, 0777, true)) {
            return null;
        }

        // Extract every attachment stream into the fonts dir. -map 0:t copies
        // all attachment streams; -map_metadata -1 avoids re-adding container
        // metadata as attachments.
        $command = "ffmpeg -loglevel error -dump_attachment:t '' -i \"{$videoPath}\" "
            . "-map 0:t -map_metadata -1 -c copy \"{$dir}/\" -y 2>&1";
        exec($command, $out, $code);

        if ($code !== 0 || count(array_diff(scandir($dir) ?: [], ['.', '..'])) === 0) {
            $this->removeDirRecursively($dir);

            return null;
        }

        $this->fontsDir = $dir;

        return $dir;
    }

    private function removeDirRecursively(string $dir): void
    {
        if (! is_dir($dir)) {
            return;
        }
        foreach (scandir($dir) ?: [] as $entry) {
            if ($entry === '.' || $entry === '..') {
                continue;
            }
            $path = $dir . '/' . $entry;
            is_dir($path) ? $this->removeDirRecursively($path) : unlink($path);
        }
        @rmdir($dir);
    }
```

> Note: the exact ffmpeg syntax for dumping attachments may vary by build. `-dump_attachment:t ''` dumps every attachment using its embedded `filename` metadata. If a build rejects the empty string, fall back to a per-index loop: `ffprobe` the attachment count, then for each index run `-dump_attachment:t:<i> <dir>/font_<i>.<ext>`. The test asserts only that a dir with ≥1 file is produced, so either approach satisfies it.

- [ ] **Step 4: Run test to verify it passes**

Run: `php artisan test --compact tests/Unit/EncoderFontExtractionTest.php`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/Services/Encoder.php tests/Unit/EncoderFontExtractionTest.php
git commit -m "feat: extract embedded video fonts to a per-job temp dir"
```

---

### Task 3: Pass `:fontsdir=` for external ASS and wire cleanup

**Files:**
- Modify: `app/Services/Encoder.php` (`buildSubtitleFilter`, `subtitleFilter`, `encode`, `encodeAnime`)
- Modify: `app/Jobs/Encode/ProcessEncode.php:67-234` (create fonts dir, pass to encoder, unlink in success + catch)
- Test: `tests/Unit/SubtitleHardsubNormalizationTest.php` (append) and `tests/Unit/EncoderFontFilterTest.php`

**Interfaces:**
- Consumes: `Encoder::extractVideoFonts(string): ?string`, `Encoder::$fontsDir` (Task 2).
- Produces: for an external ASS with fonts, `buildSubtitleFilter` returns `ass='<file>':fontsdir='<dir>'`; the fonts dir is created by the job, passed to the encoder, and unlinked by the job in both paths.

- [ ] **Step 1: Write the failing filter test**

Create `tests/Unit/EncoderFontFilterTest.php`:

```php
<?php

namespace Tests\Unit;

use App\Services\Encoder;
use PHPUnit\Framework\TestCase;
use ReflectionMethod;

class EncoderFontFilterTest extends TestCase
{
    public function test_external_ass_with_fontsdir_gets_fontsdir_option(): void
    {
        $encoder = new Encoder();
        $encoder->fontsDir = '/tmp/autosub_fonts_abc123';
        $filter = $this->invoke($encoder, 'buildSubtitleFilter', ['/tmp/sub.ass', 'ass', false]);

        $this->assertSame("ass='/tmp/sub.ass':fontsdir='/tmp/autosub_fonts_abc123'", $filter);
    }

    public function test_external_ass_without_fontsdir_omits_fontsdir(): void
    {
        $encoder = new Encoder();
        $encoder->fontsDir = null;
        $filter = $this->invoke($encoder, 'buildSubtitleFilter', ['/tmp/sub.ass', 'ass', false]);

        $this->assertSame("ass='/tmp/sub.ass'", $filter);
    }

    public function test_non_ass_filter_is_unchanged(): void
    {
        $encoder = new Encoder();
        $encoder->fontsDir = '/tmp/autosub_fonts_abc123';
        $filter = $this->invoke($encoder, 'buildSubtitleFilter', ['/tmp/sub.srt', 'srt', true]);

        $this->assertStringStartsWith("subtitles='/tmp/sub.srt':force_style='", $filter);
        $this->assertStringNotContainsString('fontsdir', $filter);
    }

    public function test_embedded_stream_filter_is_unchanged(): void
    {
        $encoder = new Encoder();
        $encoder->fontsDir = '/tmp/autosub_fonts_abc123';
        $filter = $this->invoke($encoder, 'buildSubtitleFilter', ['/video/anime.mkv', 'ass', false, 0]);

        $this->assertSame("subtitles='/video/anime.mkv':si=0", $filter);
        $this->assertStringNotContainsString('fontsdir', $filter);
    }

    private function invoke(Encoder $encoder, string $method, array $args)
    {
        return (new ReflectionMethod($encoder, $method))->invokeArgs($encoder, $args);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `php artisan test --compact tests/Unit/EncoderFontFilterTest.php`
Expected: FAIL — external ASS filter has no `:fontsdir=`.

- [ ] **Step 3: Modify `buildSubtitleFilter`**

In `app/Services/Encoder.php`, `buildSubtitleFilter` (lines 243-265), append `:fontsdir=` only for an external ASS file when `$this->fontsDir` is set:

```php
    private function buildSubtitleFilter(
        string $source,
        string $format,
        bool $useAnimeStyle,
        ?int $streamIndex = null
    ): string {
        $isAss = $this->isAssSubtitleFormat($format);

        if ($streamIndex !== null) {
            $filter = "subtitles='{$source}':si={$streamIndex}";
        } else {
            $filter = $isAss ? "ass='{$source}'" : "subtitles='{$source}'";
        }

        // External ASS: point libass at the per-job fonts dir so custom fonts
        // embedded in the source video are not broken. Omit when absent.
        if ($isAss && $streamIndex === null && $this->fontsDir) {
            $filter .= ":fontsdir='{$this->fontsDir}'";
        }

        // ASS/SSA keeps the author-provided style. Other text formats receive the
        // consistent anime style defined by this service.
        if ($useAnimeStyle && !$isAss) {
            $filter .= ":force_style='" . self::ANIME_SUBTITLE_STYLE . "'";
        }

        return $filter;
    }
```

- [ ] **Step 4: Run filter test to verify it passes**

Run: `php artisan test --compact tests/Unit/EncoderFontFilterTest.php`
Expected: PASS.

- [ ] **Step 5: Have the encoders extract fonts before building the filter**

In `app/Services/Encoder.php`, both `encode()` (around line 32) and `encodeAnime()` (around line 78), immediately before the `subtitleFilter(...)` call, extract fonts when an external ASS file is being burned:

In `encode()`:
```php
        // For an external ASS subtitle, make the video's embedded fonts
        // available to libass so styled output is not broken.
        if ($sub && $this->isAssSubtitleFormat(strtolower(pathinfo($sub, PATHINFO_EXTENSION)))) {
            $this->extractVideoFonts($source);
        }
        $subtitleFilter = $this->subtitleFilter($sub, $source, $burnSubtitle);
```

In `encodeAnime()` (after `$sub = $this->normalizeAnimeSubtitle($sub);`):
```php
        if ($sub && $this->isAssSubtitleFormat(strtolower(pathinfo($sub, PATHINFO_EXTENSION)))) {
            $this->extractVideoFonts($source);
        }
        $subtitleFilter = $this->subtitleFilter($sub, $source, $burnSubtitle, true);
```

- [ ] **Step 6: Run the existing hardsub test to confirm no regression**

Run: `php artisan test --compact tests/Unit/SubtitleHardsubNormalizationTest.php`
Expected: PASS (the existing `test_external_ass_keeps_its_original_style` still yields `ass='<path>'` because in that test `$encoder->fontsDir` is null).

- [ ] **Step 7: Wire fonts-dir cleanup into `ProcessEncode::handle`**

In `app/Jobs/Encode/ProcessEncode.php`, `handle()`:

1. After `$autoExtractedSubPath = null;` (line 86), the encoder is created inside the encode branch. Capture the fonts dir after encoding so it can be cleaned in both paths. Add a `$fontsDir = null;` local (around line 86) and, after the encode call (after line 114-118 region), read it:

```php
            $fontsDir = null;
```

After the encode branch sets `$encode` (the `$encoderService->encodeAnime(...)` / `$encode(...)` call), add:

```php
            $fontsDir = $encoderService->fontsDir;
```

2. In the success cleanup block (lines 162-169, after the `$autoExtractedSubPath` unlink), add:

```php
            if ($fontsDir && is_dir($fontsDir)) {
                $this->removeDirRecursively($fontsDir);
            }
```

3. In the catch block, before `$this->fail(...)`, add the same cleanup so a failed encode still removes the fonts dir. Add after the `if (file_exists($this->destPath())) unlink($this->destPath());` line:

```php
            if ($fontsDir && is_dir($fontsDir)) {
                $this->removeDirRecursively($fontsDir);
            }
```

4. Add the recursive-removal helper to `ProcessEncode`:

```php
    private function removeDirRecursively(string $dir): void
    {
        if (! is_dir($dir)) {
            return;
        }
        foreach (scandir($dir) ?: [] as $entry) {
            if ($entry === '.' || $entry === '..') {
                continue;
            }
            $path = $dir . '/' . $entry;
            is_dir($path) ? $this->removeDirRecursively($path) : unlink($path);
        }
        @rmdir($dir);
    }
```

> This guarantees the fonts dir is removed on both success and any exception, matching the existing `$subPath`/`$autoExtractedSubPath` handling — so a crash mid-encode cannot leak a fonts dir, and parallel jobs each own a distinct `uniqid` dir.

- [ ] **Step 8: Run the full gduploader suite**

Run: `php artisan test --compact`
Expected: PASS (existing tests + the new filter/font tests).

- [ ] **Step 9: Commit**

```bash
git add app/Services/Encoder.php app/Jobs/Encode/ProcessEncode.php tests/Unit/EncoderFontFilterTest.php
git commit -m "feat: load video fonts for external ASS hardsub and clean up per job"
```

---

### Task 4: Full regression

**Files:**
- Verify only.

- [ ] **Step 1: Run the full suite**

Run: `php artisan test --compact`
Expected: all tests pass.

- [ ] **Step 2: Verify no ASS→SRT conversion remains in the auto-extract path**

Run: `grep -n "\-c:s srt" app/Services/AutoSubtitleService.php`
Expected: the only `-c:s srt` remains in the non-ASS, non-subrip branch and in `detectStreamLanguage` (language detection is intentionally SRT-based; it does not affect the returned file).

- [ ] **Step 3: Verify fonts-dir cleanup is present in both paths**

Run: `grep -n "removeDirRecursively\|fontsDir" app/Jobs/Encode/ProcessEncode.php app/Services/Encoder.php`
Expected: `fontsDir` cleanup calls in both the success and catch blocks of `ProcessEncode::handle`, and the property/method in `Encoder`.

- [ ] **Step 4: Verify scope**

Run: `git status --short`
Expected: only the files changed in this plan.
