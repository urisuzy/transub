import runpod
import torch
import base64
import srt
from transformers import MarianMTModel, MarianTokenizer

# Inisialisasi model dan tokenizer di luar handler untuk efisiensi
model_name = "Helsinki-NLP/opus-mt-en-id"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)

print("processing resource:")
print("cuda" if torch.cuda.is_available() else "cpu")

# Pindahkan model ke GPU jika tersedia
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

# Optimalkan model untuk inferensi menggunakan TorchScript
# model = torch.jit.script(model)

def translate_batch(texts):
    """Menerjemahkan batch teks dengan mempertimbangkan konteks."""
    inputs = tokenizer(texts, return_tensors="pt", padding=True, truncation=True).to(device)
    with torch.cuda.amp.autocast():  # Mixed Precision untuk optimasi inferensi
        translated = model.generate(**inputs)
    return [tokenizer.decode(t, skip_special_tokens=True) for t in translated]

def translate_srt(srt_content, batch_size=50):  # batch size tinggi untuk memori 80GB
    """Menerjemahkan konten SRT dari Inggris ke Indonesia dengan konteks."""
    subtitles = list(srt.parse(srt_content))

    translated_subtitles = []
    for i in range(0, len(subtitles), batch_size):
        batch = subtitles[i:i + batch_size]
        batch_texts = [sub.content for sub in batch]
        translated_texts = translate_batch(batch_texts)

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

# Start Runpod serverless handler
runpod.serverless.start({"handler": handler})
