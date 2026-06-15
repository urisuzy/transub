import base64
import os
import re

import srt
from vllm import LLM, SamplingParams

# =============================================================================
# Konfigurasi model
# =============================================================================
# Model LLM kecil yang disetel khusus bahasa Indonesia (kualitas naturalness
# tertinggi untuk EN->ID). Bisa di-override lewat env var MODEL_NAME.
#   - GoToCompany/gemma2-9b-cpt-sahabatai-v1-instruct  (paling Indonesia-sentris)
#   - aisingapore/Gemma-SEA-LION-v3-9B-IT              (cakupan SEA lebih luas)
MODEL_NAME = os.environ.get(
    "MODEL_NAME", "GoToCompany/gemma2-9b-cpt-sahabatai-v1-instruct"
)

# Kuantisasi FP8 on-the-fly: muat bobot Sahabat-AI asli (tetap mempertahankan
# tuning bahasa Indonesia), runtime ~9GB VRAM, kualitas nyaris setara bf16.
# Pas untuk GPU 24GB (RTX 4090 / L4). Pilihan lain via env QUANTIZATION:
#   - "fp8"  (default) : ~9GB,  butuh GPU Ada/Hopper untuk FP8 native
#                        (di Ampere dipakai jalur weight-only Marlin).
#   - None / ""        : bf16 penuh ~18GB (hanya muat di GPU >= 32-40GB).
#   - "awq"            : ~6GB, TAPI butuh checkpoint AWQ (Sahabat-AI belum punya
#                        resmi -> harus kuantisasi sendiri dgn autoawq).
DTYPE = os.environ.get("DTYPE", "auto")
QUANTIZATION = os.environ.get("QUANTIZATION", "fp8") or None
MAX_MODEL_LEN = int(os.environ.get("MAX_MODEL_LEN", "4096"))
GPU_MEM_UTIL = float(os.environ.get("GPU_MEM_UTIL", "0.90"))

# Jendela konteks: berapa kalimat sebelum/sesudah yang diberikan ke model
# sebagai referensi makna (tidak diterjemahkan, hanya konteks).
CTX_BEFORE = int(os.environ.get("CTX_BEFORE", "2"))
CTX_AFTER = int(os.environ.get("CTX_AFTER", "1"))

# Inisialisasi model sekali di luar handler (efisien untuk serverless warm).
print(f"Loading model: {MODEL_NAME} (dtype={DTYPE}, quant={QUANTIZATION})")
llm = LLM(
    model=MODEL_NAME,
    dtype=DTYPE,
    quantization=QUANTIZATION,
    max_model_len=MAX_MODEL_LEN,
    gpu_memory_utilization=GPU_MEM_UTIL,
)

sampling_params = SamplingParams(
    temperature=0.3,   # sedikit variasi -> natural, tetap setia ke sumber
    top_p=0.9,
    max_tokens=512,
    repetition_penalty=1.05,
)

SYSTEM_PROMPT = (
    "Kamu penerjemah subtitle film profesional dari bahasa Inggris ke bahasa "
    "Indonesia. Terjemahkan dengan gaya percakapan yang natural dan luwes, "
    "bukan terjemahan kaku kata-per-kata. Sesuaikan nada bicara dengan "
    "konteks adegan. Pertahankan nama orang, tempat, dan istilah teknis. "
    "JANGAN menambahkan penjelasan, tanda kutip, atau label apa pun. "
    "Keluarkan HANYA hasil terjemahan dari kalimat target dalam satu blok teks."
)

# Penggantian kata pasca-proses (opsional).
replacements = [
    # ("Anda", "Kau"),
]

# Akhir kalimat: tanda baca + opsional kutip/kurung penutup di ujung teks.
_SENTENCE_END = re.compile(r"""[.!?…]['"”’\)\]]*\s*$""")


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
    lengkap (jauh lebih natural & benar secara tata bahasa), lalu hasilnya
    dipecah lagi ke cue aslinya pada distribute_translation().

    Mengembalikan list of list[Subtitle].
    """
    groups = []
    current = []
    for sub in subtitles:
        current.append(sub)
        if _SENTENCE_END.search(normalize_cue_text(sub.content)):
            groups.append(current)
            current = []
    if current:  # sisa tanpa tanda akhir kalimat
        groups.append(current)
    return groups


def distribute_translation(translated, group):
    """Pecah kembali kalimat terjemahan ke cue-cue aslinya.

    Pembagian proporsional terhadap jumlah kata sumber tiap cue, agar timing
    dan ritme tampilan subtitle tetap mendekati aslinya.
    """
    if len(group) == 1:
        return [translated.strip()]

    src_words = [max(1, len(normalize_cue_text(s.content).split())) for s in group]
    total_src = sum(src_words)
    tgt_words = translated.split()
    n = len(tgt_words)

    chunks = []
    idx = 0
    for k, w in enumerate(src_words):
        if k == len(src_words) - 1:
            chunk = tgt_words[idx:]
        else:
            take = round(n * w / total_src)
            take = min(take, n - idx)  # jangan over-shoot
            chunk = tgt_words[idx:idx + take]
            idx += take
        chunks.append(" ".join(chunk).strip())
    return chunks


def build_messages(sentences, i):
    """Bangun chat messages untuk kalimat ke-i dengan jendela konteks."""
    before = sentences[max(0, i - CTX_BEFORE):i]
    after = sentences[i + 1:i + 1 + CTX_AFTER]

    parts = []
    if before:
        parts.append("Konteks sebelumnya (jangan diterjemahkan):")
        parts.extend(f"- {s}" for s in before)
    if after:
        parts.append("Konteks sesudahnya (jangan diterjemahkan):")
        parts.extend(f"- {s}" for s in after)
    parts.append("")
    parts.append("Terjemahkan ke bahasa Indonesia, kalimat target ini saja:")
    parts.append(sentences[i])

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def postprocess(text):
    text = text.strip().strip('"').strip()
    for old_word, new_word in replacements:
        text = text.replace(old_word, new_word)
    return text


def translate_srt(srt_content):
    """Menerjemahkan konten SRT EN->ID dengan rekonstruksi kalimat + konteks."""
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

    # 2) Terjemahkan semua kalimat sekaligus (batched) dengan jendela konteks
    conversations = [build_messages(sentences, i) for i in range(len(sentences))]
    outputs = llm.chat(conversations, sampling_params)
    translated_sentences = [postprocess(o.outputs[0].text) for o in outputs]

    # 3) Pecah tiap kalimat terjemahan kembali ke cue aslinya
    out_subs = []
    for group, translated in zip(groups, translated_sentences):
        chunks = distribute_translation(translated, group)
        for sub, chunk in zip(group, chunks):
            sub.content = chunk
            out_subs.append(sub)

    # 4) Re-index agar penomoran rapi (tanpa lompatan setelah filter)
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
    except Exception as exc:  # base64 / encoding tidak valid
        return {"error": f"Failed to decode base64 SRT: {exc}"}

    try:
        translated_srt = translate_srt(srt_content)
    except Exception as exc:  # SRT rusak / error inferensi
        return {"error": f"Translation failed: {exc}"}

    translated_srt_base64 = base64.b64encode(
        translated_srt.encode("utf-8")
    ).decode("utf-8")

    return {"translated_srt_base64": translated_srt_base64}
