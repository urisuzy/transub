import base64
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
from translation_cache import SQLiteTranslationCache, translation_key_lock

# =============================================================================
# Konfigurasi endpoint cloud (OpenAI-compatible)
# =============================================================================
BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://157.180.30.121:20128/v1")
MODEL = os.environ.get("MODEL", "openrouter/deepseek/deepseek-v4-flash")
# API key WAJIB diisi sendiri lewat env var OPENAI_API_KEY.
API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Jumlah kalimat yang dikirim per request. Lebih besar = lebih hemat token &
# konteks antar-kalimat lebih kaya, tapi risiko misalignment baris naik.
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "100"))
# Berapa request berjalan paralel.
CONCURRENCY = int(os.environ.get("CONCURRENCY", "8"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
# Batas token output per request. Untuk chunk besar (mis. 100 baris) harus
# cukup besar agar balasan tidak terpotong (~ baris * 60 token + penomoran).
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
# Scan pass (Pass 1) cuma output JSON kecil, jadi MAX_TOKENS jauh lebih kecil
# dari MAX_TOKENS terjemahan. Cukup untuk ~CHUNK_SIZE * 20 token.
MAX_TOKENS_SCAN = int(os.environ.get("MAX_TOKENS_SCAN", "1024"))
# Retry budget kalau scan pertama return content kosong (reasoning makan
# semua token). Cuma dipakai kalau chunk tertentu gagal — adaptive retry.
MAX_TOKENS_SCAN_RETRY = int(os.environ.get("MAX_TOKENS_SCAN_RETRY", "4096"))
# Matikan thinking/reasoning (default ON di deepseek-v4-flash). Untuk tugas
# terjemahan, reasoning hampir tak berguna tapi membengkakkan output token
# (= biaya terbesar). Set DISABLE_THINKING=0 untuk mengaktifkan lagi.
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "1").lower() not in (
    "0", "false", "no", "",
)

CACHE_ENABLED = os.environ.get("TRANSLATION_CACHE_ENABLED", "1").lower() not in (
    "0", "false", "no", "",
)
CACHE_PATH = os.environ.get(
    "TRANSLATION_CACHE_PATH",
    "data/translations.sqlite3",
)
# Naikkan versi ini (atau env-nya) untuk membatalkan seluruh cache setelah
# perubahan pipeline yang tidak tercakup oleh fingerprint otomatis.
CACHE_VERSION = os.environ.get("TRANSLATION_CACHE_VERSION", "1")

SRT_HANDLER_VERSION = "1"
ASS_HANDLER_VERSION = "1"

client = OpenAI(base_url=BASE_URL, api_key=API_KEY or "EMPTY")
translation_cache = SQLiteTranslationCache(CACHE_PATH)

# Accumulator token usage per phase. Di-reset di awal translate_srt.
# Di-update via _record_usage() yang dipanggil dari _chat / _chat_with_tokens.
_TOKEN_USAGE = {
    "scan": {"calls": 0, "prompt": 0, "completion": 0,
             "reasoning": 0, "cached": 0},
    "translate": {"calls": 0, "prompt": 0, "completion": 0,
                  "reasoning": 0, "cached": 0},
}


def _record_usage(phase, resp):
    """Tambah usage dari satu API call ke _TOKEN_USAGE[phase].

    Defensive: kalau resp.usage tidak ada atau field tertentu missing,
    lewati (jangan crash).
    """
    bucket = _TOKEN_USAGE[phase]
    bucket["calls"] += 1
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    bucket["prompt"] += getattr(usage, "prompt_tokens", 0) or 0
    bucket["completion"] += getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        bucket["reasoning"] += getattr(details, "reasoning_tokens", 0) or 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details is not None:
        bucket["cached"] += getattr(prompt_details, "cached_tokens", 0) or 0


SYSTEM_PROMPT = (
    "Kamu penerjemah subtitle film profesional dari bahasa Inggris ke bahasa "
    "Indonesia. Terjemahkan dengan gaya percakapan yang natural dan luwes, "
    "bukan terjemahan kaku kata-per-kata. Sesuaikan nada bicara dengan "
    "konteks adegan. Pertahankan nama orang, tempat, dan istilah teknis.\n\n"
    "PEDOMAN TERJEMAHAN:\n\n"
    "1. IDIOM: Terjemahkan idiom dengan padanan natural Indonesia, "
    "BUKAN terjemahan kata-per-kata:\n"
    '   - "What are the chances?" -> "Emang mungkin?"  '
    '(BUKAN "Berapa kemungkinannya?")\n'
    '   - "It\'s settled." -> "Sudah kuputuskan."  '
    '(BUKAN "Sudah putus.")\n'
    '   - "Tell me about it." -> "Setuju banget."  '
    '(BUKAN "Ceritakan padaku.")\n'
    '   - "I\'m all ears." -> "Aku siap dengerin."  '
    '(BUKAN "Aku semua telinga.")\n'
    '   - "Long story short." -> "Singkat cerita."  '
    '(BUKAN "Cerita panjang pendek.")\n\n'
    "2. GAYA BICARA: Gunakan bahasa lisan wajar seperti dialog film asli.\n"
    "   - Boleh: \"nggak\", \"aja\", \"dengerin\", \"bilang\", \"liat\"\n"
    "   - Boleh: partikel \"sih\", \"kok\", \"deh\", \"kan\", \"dong\" "
    "bila sesuai konteks\n"
    "   - Hindari: bahasa formal kaku (\"tidak\" -> \"nggak\" lebih natural "
    "di percakapan)\n\n"
    "3. NADA: Sesuaikan dengan adegan -- santai untuk obrolan biasa, "
    "tegas untuk argumen, formal hanya jika tokoh memang berbicara formal.\n\n"
    "4. PANJANG: Terjemahan boleh lebih panjang atau lebih pendek dari "
    "sumber. Yang penting natural, bukan jumlah kata.\n"
    '   - Contoh: "You bet!" -> "Jelas!" (pendek)\n'
    '   - Contoh: "Sure." -> "Tentu aja." (lebih panjang)'
)

# Prompt khusus Pass 1 (scan glossary). Model cuma diminta output JSON object
# {sumber_inggris: padanan_indonesia} untuk istilah yg perlu konsisten.
SCAN_SYSTEM_PROMPT = (
    "Kamu ahli glosarium subtitle film. Dari potongan teks berikut, "
    "identifikasi SEMUA istilah yang harus diterjemahkan secara konsisten "
    "di seluruh film: nama orang, julukan/nickname, nama tempat, gelar, "
    "dan istilah khusus yang kemungkinan muncul berulang.\n\n"
    "Untuk setiap istilah, berikan padanan bahasa Indonesia yang natural "
    "dan luwes (gaya subtitle film, bukan kaku).\n\n"
    "Output WAJIB JSON object dengan format:\n"
    '{"sumber_inggris": "padanan_indonesia", ...}\n\n'
    "Aturan ketat:\n"
    "- Hanya istilah yang muncul di teks input.\n"
    "- Jangan sertakan kata umum (the, and, of, dll) atau kata yang cuma "
    "muncul sekali tanpa konteks pengulangan.\n"
    "- Kalau teks tidak punya istilah yang perlu di-glosarium-kan, output {}."
)

# Penggantian kata pasca-proses (opsional).
replacements = [
    # ("Anda", "Kau"),
]

# Baris bernomor pada output model: "12. teks" / "12) teks" / "12: teks".
_NUMBERED = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")


def postprocess(text, glossary=None):
    text = text.strip().strip('"').strip()
    for old_word, new_word in replacements:
        text = text.replace(old_word, new_word)
    # Enforce glossary (auto-detected di Pass 1). Cari EN source yang lolos
    # ke output terjemahan, ganti dengan padanan ID yang sudah di-lock.
    # Sortir by length desc supaya istilah panjang diproses dulu (menghindari
    # replace sebagian dari istilah panjang oleh istilah pendek).
    if glossary:
        for en in sorted(glossary.keys(), key=len, reverse=True):
            id_ = glossary[en]
            pattern = re.compile(rf"\b{re.escape(en)}\b", re.IGNORECASE)
            text = pattern.sub(id_, text)
    return text


def _chat(user_content):
    """Satu panggilan chat completion dengan retry."""
    last_err = None
    # OpenRouter: matikan reasoning lewat extra_body.
    extra_body = {"reasoning": {"enabled": False}} if DISABLE_THINKING else None
    for _ in range(MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                extra_body=extra_body,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            _record_usage("translate", resp)
            return resp.choices[0].message.content or ""
        except Exception as exc:  # error jaringan / API
            last_err = exc
    raise last_err


def _chat_with_tokens(user_content, system_prompt, max_tokens):
    """Sama seperti _chat tapi system prompt & max_tokens bisa di-override.

    Dipakai Pass 1 (scan glossary) yang outputnya JSON kecil dan butuh
    system prompt khusus.
    """
    last_err = None
    extra_body = {"reasoning": {"enabled": False}} if DISABLE_THINKING else None
    for _ in range(MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                temperature=TEMPERATURE,
                max_tokens=max_tokens,
                extra_body=extra_body,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            _record_usage("scan", resp)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            last_err = exc
    raise last_err


def translate_single(sentence, glossary=None):
    """Fallback: terjemahkan satu kalimat saja (dipakai jika batch gagal align)."""
    user = (
        "Terjemahkan kalimat subtitle Inggris berikut ke bahasa Indonesia. "
        "Keluarkan HANYA terjemahannya, tanpa label atau penjelasan:\n\n"
        + sentence
    )
    cache_key = _step_cache_identity(
        "single", {"request": user, "glossary": glossary or {}}
    )
    cached = _get_cached_translation_step(cache_key, 1)
    if cached is not None:
        return cached[0]

    with translation_key_lock(cache_key):
        cached = _get_cached_translation_step(cache_key, 1)
        if cached is not None:
            return cached[0]

        translated = postprocess(_chat(user), glossary)
        _set_cached_step(cache_key, "single", [translated])
        return translated


def scan_terms(chunk):
    """Pass 1: ekstrak glossary {EN: ID} dari sebuah chunk kalimat sumber.

    Sequential (dipanggil dari build_glossary). Kalau JSON model rusak
    atau kosong, kembalikan {} — defensive: pipeline utama tetap jalan,
    cuma kehilangan konsistensi untuk istilah di chunk itu.

    Adaptive retry: kalau panggilan murah (MAX_TOKENS_SCAN) return content
    kosong (biasanya karena reasoning makan semua token budget), retry
    SEKALI dengan MAX_TOKENS_SCAN_RETRY. Cost: 1 extra call HANYA kalau
    yang pertama gagal — best case zero overhead.
    """
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
    user = (
        f"Identifikasi istilah yang harus konsisten dari {len(chunk)} baris "
        "subtitle Inggris berikut:\n\n" + numbered
    )
    cache_key = _step_cache_identity("scan", {"request": user})
    cached = _get_cached_scan(cache_key)
    if cached is not None:
        return cached

    with translation_key_lock(cache_key):
        cached = _get_cached_scan(cache_key)
        if cached is not None:
            return cached

        text = _chat_with_tokens(user, SCAN_SYSTEM_PROMPT, MAX_TOKENS_SCAN)

        # Adaptive retry: content kosong / no-JSON biasanya artinya reasoning
        # makan semua MAX_TOKENS_SCAN. Retry dengan budget lebih besar.
        if not text or "{" not in text:
            print(f"  scan: empty response, retrying with "
                  f"{MAX_TOKENS_SCAN_RETRY} tokens")
            text = _chat_with_tokens(user, SCAN_SYSTEM_PROMPT, MAX_TOKENS_SCAN_RETRY)

        if not text:
            print(f"  scan: still empty after retry (chunk size {len(chunk)})")
            return {}

        # Cari JSON object di output. Model kadang membungkus dengan
        # ```json ... ``` atau teks preamble.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            print(f"  scan: no JSON in response (chunk size {len(chunk)}): {text!r}")
            return {}
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as exc:
            print(f"  scan: JSON parse error ({exc}): {text!r}")
            return {}
        if not isinstance(data, dict):
            print(f"  scan: JSON is not an object (chunk size {len(chunk)})")
            return {}

        # Sanitasi: hanya str -> str, key non-empty, value non-empty.
        cleaned = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                cleaned[k.strip()] = v.strip()
        _set_cached_step(cache_key, "scan", cleaned)
        return cleaned


def build_glossary(chunks):
    """Pass 1 (orchestrator): scan semua chunk, gabung jadi satu glossary.

    Sequential karena scan murah (output JSON kecil) dan biar 'first
    writer wins' — kalau istilah muncul di chunk 1 dan chunk 7, versi
    chunk 1 yang dipakai. Iterasi langsung (bukan paralel) supaya
    log urut dan debugging mudah.
    """
    merged = {}
    for idx, chunk in enumerate(chunks, start=1):
        terms = scan_terms(chunk)
        for en, id_ in terms.items():
            # Dedupe case-insensitive: kalau sudah ada entri dengan lower(en)
            # yang sama, JANGAN timpa. First-seen wins.
            if en.lower() not in {k.lower() for k in merged}:
                merged[en] = id_
        print(f"  scan chunk {idx}/{len(chunks)}: +{len(terms)} term(s), "
              f"glossary total: {len(merged)}")
    return merged


def translate_chunk(chunk, glossary=None):
    """Terjemahkan sekumpulan kalimat berurutan dalam satu request (bernomor).

    Kalimat dalam satu chunk saling jadi konteks. Output diparse balik per
    nomor. Jika jumlah/penomoran tidak cocok (mis. model menggabung baris atau
    balasan terpotong), chunk DIBELAH DUA dan dicoba ulang secara rekursif --
    bukan langsung jatuh ke per-kalimat -- supaya chunk besar tetap hemat
    request. Per-kalimat hanya dipakai sebagai dasar (chunk berukuran 1).

    `glossary` (opsional): dict {EN: ID_locked} dari Pass 1. Disuntik ke
    user prompt agar model pakai terjemahan konsisten, dan ditegakkan
    ulang di postprocess() sebagai safety net.
    """
    n = len(chunk)
    if n == 1:
        return [translate_single(chunk[0], glossary)]

    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
    glossary_block = ""
    if glossary:
        lines = [f'- "{en}" -> "{id_}"' for en, id_ in sorted(glossary.items())]
        glossary_block = (
            "ISTILAH TETAP (WAJIB konsisten di seluruh subtitle ini):\n"
            + "\n".join(lines)
            + "\n\n"
        )
    user = (
        f"Terjemahkan {n} baris subtitle Inggris berikut ke bahasa Indonesia.\n"
        "Aturan ketat:\n"
        f"- Keluarkan TEPAT {n} baris.\n"
        "- Pertahankan nomor urut di depan tiap baris (format: \"N. teks\").\n"
        "- Satu baris input = satu baris output. JANGAN menggabung atau "
        "memecah baris.\n"
        "- Natural dan luwes; jangan menambahkan penjelasan apa pun.\n\n"
        + glossary_block
        + numbered
    )
    cache_key = _step_cache_identity(
        "chunk", {"request": user, "glossary": glossary or {}}
    )
    cached = _get_cached_translation_step(cache_key, n)
    if cached is not None:
        return cached

    with translation_key_lock(cache_key):
        cached = _get_cached_translation_step(cache_key, n)
        if cached is not None:
            return cached

        text = _chat(user)

        parsed = {}
        for line in text.splitlines():
            m = _NUMBERED.match(line)
            if m:
                parsed[int(m.group(1))] = m.group(2).strip()

        results = [parsed.get(i + 1) for i in range(n)]
        if any(r is None or r == "" for r in results):
            # Penomoran tidak utuh -> belah dua dan coba ulang tiap separuh.
            mid = n // 2
            print(f"  chunk align gagal (n={n}), pecah jadi {mid}+{n - mid}")
            return (
                translate_chunk(chunk[:mid], glossary)
                + translate_chunk(chunk[mid:], glossary)
            )

        translated = [postprocess(r, glossary) for r in results]
        _set_cached_step(cache_key, "chunk", translated)
        return translated


def _build_usage_summary():
    """Bangun dict ringkasan token usage per-phase + total.

    Shape: {
      'scan':     {calls, prompt, completion, reasoning, cached},
      'translate':{calls, prompt, completion, reasoning, cached},
      'total':    {calls, prompt, completion, reasoning, cached},
    }
    """
    total = {"calls": 0, "prompt": 0, "completion": 0,
             "reasoning": 0, "cached": 0}
    summary = {}
    for phase, b in _TOKEN_USAGE.items():
        summary[phase] = dict(b)
        for k in total:
            total[k] += b[k]
    summary["total"] = total
    return summary


def _empty_usage_summary():
    empty = {"calls": 0, "prompt": 0, "completion": 0,
             "reasoning": 0, "cached": 0}
    return {
        "scan": dict(empty),
        "translate": dict(empty),
        "total": dict(empty),
    }


def _print_usage_summary():
    """Cetak ringkasan token usage per phase + total."""
    summary = _build_usage_summary()
    print("\n=== TOKEN USAGE ===")
    for phase in ("scan", "translate", "total"):
        b = summary[phase]
        if b["calls"] == 0 and phase == "total":
            continue
        label = phase.upper() if phase == "total" else phase
        print(f"  {label:9s}: {b['calls']:3d} calls | "
              f"prompt={b['prompt']:>7,} | "
              f"completion={b['completion']:>7,} "
              f"(reasoning={b['reasoning']:>6,}, cached={b['cached']:>6,})")


def _translation_cache_identity(
    content,
    subtitle_format="srt",
    handler_version=SRT_HANDLER_VERSION,
):
    """Bangun cache key dari sumber dan semua input yang memengaruhi hasil."""
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


def _get_cached_translation(cache_key):
    try:
        return translation_cache.get(cache_key)
    except Exception as exc:
        # Cache tidak boleh membuat layanan penerjemahan utama gagal.
        print(f"Translation cache read failed: {exc}")
        return None


def _set_cached_translation(cache_key, source_sha256, translated_srt):
    try:
        translation_cache.set(
            cache_key=cache_key,
            source_sha256=source_sha256,
            model=MODEL,
            translated_srt=translated_srt,
        )
    except Exception as exc:
        print(f"Translation cache write failed: {exc}")


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


def _get_cached_scan(cache_key):
    cached = _get_cached_step(cache_key)
    if not isinstance(cached, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and key
        and value
        and key == key.strip()
        and value == value.strip()
        for key, value in cached.items()
    ):
        return None
    print(f"Scan cache hit: {cache_key[:12]}")
    return cached


def _get_cached_translation_step(cache_key, expected_count):
    cached = _get_cached_step(cache_key)
    if not (
        isinstance(cached, list)
        and len(cached) == expected_count
        and all(isinstance(value, str) for value in cached)
    ):
        return None
    print(f"Translation cache hit: {cache_key[:12]}")
    return cached


def _set_cached_step(cache_key, step, result):
    if not CACHE_ENABLED:
        return
    try:
        translation_cache.set_step(cache_key, step, result)
    except Exception as exc:
        print(f"LLM step cache write failed: {exc}")


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


def handler(event):
    """Entry point RunPod. Input/output teks SRT yang dienkode base64."""
    input_data = event.get("input", {})
    srt_text_base64 = input_data.get("srt_text_base64", "")

    if not srt_text_base64:
        return {"error": "Base64-encoded SRT text is required."}

    try:
        srt_content = base64.b64decode(srt_text_base64).decode("utf-8")
    except Exception as exc:
        return {"error": f"Failed to decode base64 SRT: {exc}"}

    try:
        result = translate_srt(srt_content)
    except Exception as exc:
        return {"error": f"Translation failed: {exc}"}

    translated_srt = result["srt"]
    translated_srt_base64 = base64.b64encode(
        translated_srt.encode("utf-8")
    ).decode("utf-8")

    return {
        "translated_srt_base64": translated_srt_base64,
        "token_usage": result["token_usage"],
        "cached": result.get("cached", False),
    }
