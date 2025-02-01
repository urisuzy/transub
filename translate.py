import torch
import base64
import srt
from transformers import MarianMTModel, MarianTokenizer
import platform
import os
import cpuinfo
import datetime
import re

# Inisialisasi model dan tokenizer di luar handler untuk efisiensi
model_name = "Helsinki-NLP/opus-mt-en-id"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)

has_gpu = torch.cuda.is_available()
resource_type = has_gpu and "cuda" or "cpu"


print(f"CPU: " + cpuinfo.get_cpu_info()['brand_raw'])
print(f"CPU CORE: {os.cpu_count()}")
print(f"GPU: {(has_gpu and torch.cuda.get_device_name()) or 'No GPU'}")
print(f"processing resource: {resource_type}")

# Pindahkan model ke GPU jika tersedia
device = torch.device(resource_type)
model.to(device)

# Array kata-kata yang ingin diganti dan penggantinya
replacements = [
    ("Anda", "Kau"),
    # Tambahkan kata lain di sini
]

# Optimalkan model untuk inferensi menggunakan TorchScript
# model = torch.jit.script(model)

def remove_empty_subtitles(subtitles):
    """Menghapus subtitle yang sub.content-nya kosong."""
    return [sub for sub in subtitles if sub.content.strip()]

def remove_hearing_impaired(subtitles):
    """Menghapus teks HEARING IMPAIRED dari subtitle."""
    for sub in subtitles:
        sub.content = re.sub(r'\([^)]*\)', '', sub.content)
        sub.content = re.sub(r'\[[^]]*\]', '', sub.content)
        # print(sub.content)

    return subtitles

def translate_batch(texts):
    """Menerjemahkan batch teks dengan mempertimbangkan konteks."""
    inputs = tokenizer(texts, return_tensors="pt", padding=True, truncation=True).to(device)
    with torch.amp.autocast("cuda"):  # Mixed Precision untuk optimasi inferensi
        translated = model.generate(**inputs)
    translated_texts = [tokenizer.decode(t, skip_special_tokens=True) for t in translated]


    # Post-processing untuk mengganti kata-kata sesuai array replacements
    for i in range(len(translated_texts)):
        for old_word, new_word in replacements:
            translated_texts[i] = translated_texts[i].replace(old_word, new_word)

    return translated_texts

def translate_srt(srt_content, batch_size=200):  # batch size tinggi untuk memori 80GB
    """Menerjemahkan konten SRT dari Inggris ke Indonesia dengan konteks."""
    subtitles = list(srt.parse(srt_content))
    print(f"Total subtitles before filter: {len(subtitles)}")

    # Menghapus teks HEARING IMPAIRED
    subtitles = remove_hearing_impaired(subtitles)

    # Menghapus subtitle yang kosong
    subtitles = remove_empty_subtitles(subtitles)
    
    print(f"Total subtitles after filter: {len(subtitles)}")

    translated_subtitles = []
    for i in range(0, len(subtitles), batch_size):
        print(f"Translating subtitles {i + 1} to {min(i + batch_size, len(subtitles))}...")
        print(f"loop {i} batch size {batch_size}")
        print("Translating...")
        starttime = datetime.datetime.now()
        batch = subtitles[i:i + batch_size]
        batch_texts = [sub.content for sub in batch]
        translated_texts = translate_batch(batch_texts)
        print(f"Translated {len(translated_texts)} subtitles.")
        print(f"Duration: {datetime.datetime.now() - starttime}")
        print("=====================================================")

        for sub, translated_text in zip(batch, translated_texts):
            sub.content = translated_text
            translated_subtitles.append(sub)

    return srt.compose(translated_subtitles)

def handler(event):
    # Ambil teks SRT yang dienkode base64 dari JSON payload
    input_data = event.get("input", {})
    srt_text_base64 = input_data.get("srt_text_base64", "")

    if not srt_text_base64:
        return {"error": "Base64-encoded SRT text is required."}

    # Decode teks base64 menjadi SRT biasa
    srt_content = base64.b64decode(srt_text_base64).decode("utf-8")

    # Terjemahkan SRT
    translated_srt = translate_srt(srt_content)

    # Encode hasil terjemahan SRT ke base64 untuk output
    translated_srt_base64 = base64.b64encode(translated_srt.encode("utf-8")).decode("utf-8")

    # Return hasil terjemahan sebagai base64
    return {
        "translated_srt_base64": translated_srt_base64
    }