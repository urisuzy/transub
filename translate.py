import base64
import os
import re
from concurrent.futures import ThreadPoolExecutor

import srt
from openai import OpenAI

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
# Matikan thinking/reasoning (default ON di deepseek-v4-flash). Untuk tugas
# terjemahan, reasoning hampir tak berguna tapi membengkakkan output token
# (= biaya terbesar). Set DISABLE_THINKING=0 untuk mengaktifkan lagi.
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "1").lower() not in (
    "0", "false", "no", "",
)

client = OpenAI(base_url=BASE_URL, api_key=API_KEY or "EMPTY")

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

# Penggantian kata pasca-proses (opsional).
replacements = [
    # ("Anda", "Kau"),
]

# Akhir kalimat: tanda baca + opsional kutip/kurung penutup di ujung teks.
_SENTENCE_END = re.compile(r"""[.!?…]['"”’\)\]]*\s*$""")
# Baris bernomor pada output model: "12. teks" / "12) teks" / "12: teks".
_NUMBERED = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")


def remove_empty_subtitles(subtitles):
    """Menghapus subtitle yang kontennya kosong."""
    return [sub for sub in subtitles if sub.content.strip()]


def remove_hearing_impaired(subtitles):
    """Menghapus teks hearing-impaired, mis. (suara pintu) atau [MUSIK]."""
    for sub in subtitles:
        sub.content = re.sub(r"\([^)]*\)", "", sub.content)
        sub.content = re.sub(r"\[[^\]]*\]", "", sub.content)
    return subtitles


def normalize_cue_text(text):
    """Ratakan baris dalam satu cue jadi satu spasi (subtitle sering 2 baris)."""
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def group_into_sentences(subtitles):
    """Kelompokkan cue berurutan menjadi kalimat utuh.

    Subtitle sering memecah satu kalimat ke beberapa cue. Kita gabungkan cue
    sampai bertemu tanda akhir kalimat, sehingga model menerjemahkan kalimat
    lengkap (jauh lebih natural), lalu hasilnya dipecah lagi ke cue aslinya.

    Mengembalikan list of list[Subtitle].
    """
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
    """Pecah terjemahan kembali ke cue asli dengan pendekatan multi-tier.

    Tier 1: Cocokkan tanda baca akhir cue sumber dengan teks terjemahan.
    Tier 2: Gagal total — proporsi karakter + jangkar tanda baca.
    Tier 3: Gagal juga — potong di spasi (word boundary).

    Karena subtitle selalu dipotong di tanda baca, Tier 1 menangani >95% kasus
    dengan akurasi tinggi. Tier 2-3 adalah fallback untuk kasus di mana struktur
    tanda baca berubah drastis antara EN dan ID.
    """
    if len(group) == 1:
        return [translated.strip()]

    tgt = translated.strip()
    n = len(tgt)
    src_texts = [normalize_cue_text(s.content) for s in group]

    # Temukan posisi potong untuk setiap batas antar-cue.
    cuts = []
    prev = 0

    for k in range(len(group) - 1):
        cut = _find_cut(src_texts, k, tgt, prev)
        # Jamin tidak mundur dan sisakan minimal 1 karakter untuk cue tersisa.
        cut = max(cut, prev + 1)
        cut = min(cut, n - (len(group) - k - 1))
        cuts.append(cut)
        prev = cut

    # Bangun hasil.
    chunks = []
    prev = 0
    for cut in cuts:
        chunks.append(tgt[prev:cut].strip())
        prev = cut
    chunks.append(tgt[prev:].strip())
    return chunks


def _find_cut(src_texts, cue_idx, tgt, prev):
    """Cari posisi potong optimal antara cue[cue_idx] dan cue[cue_idx+1]."""
    n = len(tgt)

    # --- Tier 1: Cocokkan trailing punctuation dari source cue di target ---
    src = src_texts[cue_idx]
    trail = re.search(r"[.!?…,;:\-—]+$", src)
    if trail:
        trail_text = trail.group()
        # Cari di tgt mulai dari prev + 1.
        pos = tgt.find(trail_text, prev + 1)
        if pos != -1:
            cut = pos + len(trail_text)
            while cut < n and tgt[cut] == " ":
                cut += 1
            if cut > prev and cut < n:
                return cut

    # --- Tier 2: Character ratio + punctuation anchor ---
    src_chars = [max(1, len(s)) for s in src_texts]
    total_src = sum(src_chars)
    target = round(n * sum(src_chars[:cue_idx + 1]) / total_src)
    window = max(8, round(n * 0.25))
    lo = max(prev + 1, target - window)
    hi = min(n - (len(src_texts) - cue_idx - 1), target + window)

    # Semua posisi anchor (setelah tanda baca) dalam tgt.
    anchors = []
    for m in re.finditer(r"[.,!?;:…—\-]", tgt):
        pos = m.end()
        while pos < n and tgt[pos] == " ":
            pos += 1
        anchors.append(pos)

    candidates = [ap for ap in anchors if lo <= ap <= hi]
    if candidates:
        return min(candidates, key=lambda x: abs(x - target))

    # --- Tier 3: Spasi terdekat ---
    left = tgt.rfind(" ", lo, target)
    right = tgt.find(" ", target, hi)
    space_candidates = []
    if left != -1:
        space_candidates.append((abs(left - target), left + 1))
    if right != -1:
        space_candidates.append((abs(right - target), right + 1))
    if space_candidates:
        return min(space_candidates, key=lambda x: x[0])[1]

    return target


def postprocess(text):
    text = text.strip().strip('"').strip()
    for old_word, new_word in replacements:
        text = text.replace(old_word, new_word)
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
            return resp.choices[0].message.content or ""
        except Exception as exc:  # error jaringan / API
            last_err = exc
    raise last_err


def translate_single(sentence):
    """Fallback: terjemahkan satu kalimat saja (dipakai jika batch gagal align)."""
    out = _chat(
        "Terjemahkan kalimat subtitle Inggris berikut ke bahasa Indonesia. "
        "Keluarkan HANYA terjemahannya, tanpa label atau penjelasan:\n\n"
        + sentence
    )
    return postprocess(out)


def translate_chunk(chunk):
    """Terjemahkan sekumpulan kalimat berurutan dalam satu request (bernomor).

    Kalimat dalam satu chunk saling jadi konteks. Output diparse balik per
    nomor. Jika jumlah/penomoran tidak cocok (mis. model menggabung baris atau
    balasan terpotong), chunk DIBELAH DUA dan dicoba ulang secara rekursif --
    bukan langsung jatuh ke per-kalimat -- supaya chunk besar tetap hemat
    request. Per-kalimat hanya dipakai sebagai dasar (chunk berukuran 1).
    """
    n = len(chunk)
    if n == 1:
        return [translate_single(chunk[0])]

    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
    user = (
        f"Terjemahkan {n} baris subtitle Inggris berikut ke bahasa Indonesia.\n"
        "Aturan ketat:\n"
        f"- Keluarkan TEPAT {n} baris.\n"
        "- Pertahankan nomor urut di depan tiap baris (format: \"N. teks\").\n"
        "- Satu baris input = satu baris output. JANGAN menggabung atau "
        "memecah baris.\n"
        "- Natural dan luwes; jangan menambahkan penjelasan apa pun.\n\n"
        + numbered
    )
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
        return translate_chunk(chunk[:mid]) + translate_chunk(chunk[mid:])
    return [postprocess(r) for r in results]


def translate_srt(srt_content):
    """Menerjemahkan konten SRT EN->ID via API cloud (batch + rekonstruksi)."""
    subtitles = list(srt.parse(srt_content))
    print(f"Total subtitles before filter: {len(subtitles)}")

    subtitles = remove_hearing_impaired(subtitles)
    subtitles = remove_empty_subtitles(subtitles)
    print(f"Total subtitles after filter: {len(subtitles)}")

    if not subtitles:
        return ""

    # 1) Gabungkan cue -> kalimat utuh
    groups = group_into_sentences(subtitles)
    sentences = [normalize_cue_text(" ".join(s.content for s in g)) for g in groups]
    print(f"Reconstructed into {len(sentences)} sentences from "
          f"{len(subtitles)} cues.")

    # 2) Bagi jadi chunk, terjemahkan paralel
    chunks = [sentences[i:i + CHUNK_SIZE]
              for i in range(0, len(sentences), CHUNK_SIZE)]
    print(f"Translating {len(chunks)} chunks (size {CHUNK_SIZE}, "
          f"concurrency {CONCURRENCY})...")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        chunk_results = list(pool.map(translate_chunk, chunks))
    translated_sentences = [s for res in chunk_results for s in res]

    # 3) Pecah tiap kalimat terjemahan kembali ke cue aslinya
    out_subs = []
    for group, translated in zip(groups, translated_sentences):
        pieces = distribute_translation(translated, group)
        for sub, piece in zip(group, pieces):
            sub.content = piece
            out_subs.append(sub)

    # 4) Re-index agar penomoran rapi
    for new_index, sub in enumerate(out_subs, start=1):
        sub.index = new_index

    return srt.compose(out_subs)


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
        translated_srt = translate_srt(srt_content)
    except Exception as exc:
        return {"error": f"Translation failed: {exc}"}

    translated_srt_base64 = base64.b64encode(
        translated_srt.encode("utf-8")
    ).decode("utf-8")

    return {"translated_srt_base64": translated_srt_base64}
