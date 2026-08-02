# auto-leech: Carry ASS Extension End-to-End Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop force-labeling subtitle bytes as `.srt` in auto-leech so an ASS reference is carried into transub and a translated ASS is saved back as `.ass`.

**Architecture:** Detect the subtitle format by sniffing the content (both `[Script Info]` and `[Events]` present, case/BOM-insensitive → `ass`, else `srt`), then use that extension when writing the reference to Minio, when sending the multipart filename to transub, and when persisting the translated output. Content stays opaque bytes; only the extension label changes.

**Tech Stack:** PHP 8.2, Laravel 12, Pest, Minio (disk `minio`).

## Global Constraints

- Detect format from content bytes (do NOT trust the remote codec report — `downloadSoftsub` does not reliably return format).
- Sniff rule (exact): content contains both `[Script Info]` and `[Events]` (case-insensitive, BOM-tolerated) → extension `.ass`; otherwise `.srt`.
- Subtitle content is opaque bytes — no parsing, no conversion, no rewriting.
- No new dependency.
- Preserve existing job flow, queue names, and Minio paths.
- Do not modify `transub` or `transub-api` (already pass ASS through).

---

### Task 1: Add a subtitle-format sniff helper

**Files:**
- Create: `app/Services/SubtitleFormatDetector.php`
- Test: `tests/Unit/SubtitleFormatDetectorTest.php`

**Interfaces:**
- Consumes: nothing (standalone).
- Produces: `App\Services\SubtitleFormatDetector::extensionFor(string $content): string` returning `'ass'` or `'srt'`.

- [ ] **Step 1: Write the failing test**

Create `tests/Unit/SubtitleFormatDetectorTest.php`:

```php
<?php

namespace Tests\Unit;

use App\Services\SubtitleFormatDetector;
use PHPUnit\Framework\TestCase;

class SubtitleFormatDetectorTest extends TestCase
{
    private SubtitleFormatDetector $detector;

    protected function setUp(): void
    {
        parent::setUp();
        $this->detector = new SubtitleFormatDetector();
    }

    public function test_detects_ass_from_script_info_and_events(): void
    {
        $this->assertSame('ass', $this->detector->extensionFor(
            "[Script Info]\nTitle: x\n[Events]\nFormat: Layer, Text\n"
        ));
    }

    public function test_detects_ass_case_insensitively(): void
    {
        $this->assertSame('ass', $this->detector->extensionFor(
            "[script info]\n[events]\n"
        ));
    }

    public function test_detects_ass_with_utf8_bom(): void
    {
        $this->assertSame('ass', $this->detector->extensionFor(
            "\xEF\xBB\xBF[Script Info]\n[Events]\n"
        ));
    }

    public function test_defaults_to_srt_when_events_missing(): void
    {
        $this->assertSame('srt', $this->detector->extensionFor(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
        ));
    }

    public function test_defaults_to_srt_when_script_info_missing(): void
    {
        $this->assertSame('srt', $this->detector->extensionFor(
            "[Events]\nFormat: Layer, Text\n"
        ));
    }

    public function test_defaults_to_srt_on_empty_content(): void
    {
        $this->assertSame('srt', $this->detector->extensionFor(''));
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `php artisan test --compact tests/Unit/SubtitleFormatDetectorTest.php`
Expected: FAIL — `Class "App\Services\SubtitleFormatDetector" not found`.

- [ ] **Step 3: Write minimal implementation**

Create `app/Services/SubtitleFormatDetector.php`:

```php
<?php

namespace App\Services;

class SubtitleFormatDetector
{
    /**
     * Sniff whether subtitle content is ASS (contains both [Script Info] and
     * [Events], case/BOM-insensitive) or plain SRT. Content is otherwise
     * treated as opaque bytes.
     */
    public function extensionFor(string $content): string
    {
        $sections = [];
        foreach (preg_split('/\r\n|\r|\n/', $content) ?: [] as $line) {
            $trimmed = ltrim($line, "\xEF\xBB\xBF \t");
            if (str_starts_with($trimmed, '[') && str_ends_with($trimmed, ']')) {
                $sections[] = strtolower($trimmed);
            }
        }

        return in_array('[script info]', $sections, true)
            && in_array('[events]', $sections, true)
                ? 'ass'
                : 'srt';
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `php artisan test --compact tests/Unit/SubtitleFormatDetectorTest.php`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/Services/SubtitleFormatDetector.php tests/Unit/SubtitleFormatDetectorTest.php
git commit -m "feat: add subtitle format detector (ass vs srt)"
```

---

### Task 2: Persist translated output with the real extension

**Files:**
- Modify: `app/Services/SubtitleSearchService.php:434-470` (`translateSubtitle`)
- Test: `tests/Unit/SubtitleSearchServiceTranslateExtensionTest.php`

**Interfaces:**
- Consumes: `SubtitleFormatDetector::extensionFor(string): string` (Task 1), `TranssubService::translate(string $content, string $filename = 'subtitle.srt'): string` (exists).
- Produces: translated output saved to `subtitles/translated_<uuid>.<ext>` where `<ext>` is the sniffed extension; `SubtitleSearch` updated as before.

- [ ] **Step 1: Write the failing test**

Create `tests/Unit/SubtitleSearchServiceTranslateExtensionTest.php`:

```php
<?php

namespace Tests\Unit;

use App\Models\SubtitleSearch;
use App\Services\SubtitleFormatDetector;
use App\Services\SubtitleSearchService;
use App\Services\TranssubService;
use Illuminate\Foundation\Testing\TestCase;
use Illuminate\Support\Facades\Storage;
use Mockery;

class SubtitleSearchServiceTranslateExtensionTest extends TestCase
{
    public function test_translated_output_uses_sniffed_extension(): void
    {
        Storage::fake('minio');
        Storage::disk('minio')->put('subtitle-references/ref.ass', "[Script Info]\n[Events]\n");

        $search = new SubtitleSearch(['reference' => 'subtitle-references/ref.ass']);

        $transsub = Mockery::mock(TranssubService::class);
        $transsub->shouldReceive('translate')
            ->once()
            ->with(Mockery::any(), Mockery::any())
            ->andReturn("[Script Info]\n[Events]\nDialogue: 0,0,0,Default,,0,0,0,,Halo\n");

        $service = (new SubtitleSearchService())->setTranssubService($transsub);

        $result = $service->translateSubtitle($search);

        // The saved file must end in .ass, not .srt.
        $path = Storage::disk('minio')->files('subtitles')[0] ?? null;
        $this->assertNotNull($path);
        $this->assertStringEndsWith('.ass', $path);
        $this->assertSame(['input_characters' => 24, 'output_characters' => 71], $result);
    }

    public function test_srt_reference_still_saves_as_srt(): void
    {
        Storage::fake('minio');
        Storage::disk('minio')->put('subtitle-references/ref.srt', "1\n00:00:00,000 --> 00:00:01,000\nHello\n");

        $search = new SubtitleSearch(['reference' => 'subtitle-references/ref.srt']);

        $transsub = Mockery::mock(TranssubService::class);
        $transsub->shouldReceive('translate')->once()->andReturn("1\n00:00:00,000 --> 00:00:01,000\nHalo\n");

        $service = (new SubtitleSearchService())->setTranssubService($transsub);

        $service->translateSubtitle($search);

        $path = Storage::disk('minio')->files('subtitles')[0] ?? null;
        $this->assertNotNull($path);
        $this->assertStringEndsWith('.srt', $path);
    }
}
```

> Note: `setTranssubService` is a test-injection setter the implementer must add to `SubtitleSearchService` (mirror existing `transsub` property usage in `translateSubtitle`). If the class already injects `TranssubService` differently, adapt the test to the existing injection mechanism and verify `translateSubtitle` reads the sniffed extension.

- [ ] **Step 2: Run test to verify it fails**

Run: `php artisan test --compact tests/Unit/SubtitleSearchServiceTranslateExtensionTest.php`
Expected: FAIL — output still saved as `translated_<uuid>.srt` (or `setTranssubService` missing).

- [ ] **Step 3: Implement the extension change**

In `app/Services/SubtitleSearchService::translateSubtitle` (currently lines 434-470), replace the filename/extension logic:

```php
        $filename = basename($search->reference);
        $translated = $this->transsub->translate($referenceContent, $filename);

        $extension = (new SubtitleFormatDetector())->extensionFor($translated);
        $minioPath = 'subtitles/translated_'.Str::uuid().'.'.$extension;
        Storage::disk('minio')->put($minioPath, $translated);
```

Add the test-injection setter if needed:

```php
    public function setTranssubService(TranssubService $transsub): self
    {
        $this->transsub = $transsub;

        return $this;
    }
```

> The filename sent to transub stays `basename($search->reference)` so the reference's real extension (already fixed in Task 3) is what reaches the multipart upload. The output extension is sniffed from the **translated** content, which is the ground truth.

- [ ] **Step 4: Run test to verify it passes**

Run: `php artisan test --compact tests/Unit/SubtitleSearchServiceTranslateExtensionTest.php`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/Services/SubtitleSearchService.php tests/Unit/SubtitleSearchServiceTranslateExtensionTest.php
git commit -m "feat: persist translated subtitle with detected extension"
```

---

### Task 3: Save ENG references with the real extension

**Files:**
- Modify: `app/Jobs/EpisodeProject/PostEpisode.php:160` (reference save)
- Modify: `app/Jobs/MovieProject/PostMovie.php:207` (reference save)
- Modify: `app/Jobs/AnimeProject/PostAnime.php:141` (reference save)

**Interfaces:**
- Consumes: `SubtitleFormatDetector::extensionFor(string): string` (Task 1).
- Produces: reference saved to `subtitle-references/<uuid>.<ext>` where `<ext>` is sniffed from the downloaded ENG content. Downstream `translateSubtitle` then reads `basename($search->reference)` with the real extension.

- [ ] **Step 1: Apply the reference-path change to PostEpisode**

In `app/Jobs/EpisodeProject/PostEpisode.php`, the current code (line 160) is:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.srt';
```

Replace with:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.'
            .(new \App\Services\SubtitleFormatDetector())->extensionFor($engContent);
```

- [ ] **Step 2: Apply the same change to PostMovie**

In `app/Jobs/MovieProject/PostMovie.php`, current line 207:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.srt';
```

Replace with:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.'
            .(new \App\Services\SubtitleFormatDetector())->extensionFor($engContent);
```

- [ ] **Step 3: Apply the same change to PostAnime**

In `app/Jobs/AnimeProject/PostAnime.php`, current line 141:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.srt';
```

Replace with:

```php
        $refPath = 'subtitle-references/'.Str::uuid().'.'
            .(new \App\Services\SubtitleFormatDetector())->extensionFor($engContent);
```

In all three, `$engContent` is the variable already holding the downloaded ENG subtitle bytes (`$uploader->downloadSoftsub($project->uploader_download_path, 'eng')`).

- [ ] **Step 4: Run the existing test suite to confirm no regression**

Run: `php artisan test --compact`
Expected: PASS (existing tests unaffected; the jobs are covered by existing project tests).

- [ ] **Step 5: Commit**

```bash
git add app/Jobs/EpisodeProject/PostEpisode.php app/Jobs/MovieProject/PostMovie.php app/Jobs/AnimeProject/PostAnime.php
git commit -m "feat: save subtitle references with detected extension"
```

---

### Task 4: Full regression

**Files:**
- Verify only.

- [ ] **Step 1: Run the full suite**

Run: `php artisan test --compact`
Expected: all tests pass.

- [ ] **Step 2: Verify no stray `.srt` hardcoding remains for subtitle output**

Run: `grep -rn "translated_.*\.srt" app/ && grep -rn "subtitle-references/.*\.srt" app/`
Expected: no matches for translated output; reference paths now use the detector. (The `TranssubService` default `$filename = 'subtitle.srt'` is intentionally left as a harmless default fallback.)

- [ ] **Step 3: Verify scope**

Run: `git status --short`
Expected: only the files changed in this plan.
