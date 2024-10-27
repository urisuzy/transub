from transformers import MarianMTModel, MarianTokenizer
import srt
import os
import json
import base64

# Inisialisasi model dan tokenizer satu kali di luar handler untuk efisiensi
model_name = "Helsinki-NLP/opus-mt-en-id"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)

def translate_batch(texts):
    """Menerjemahkan batch teks dengan mempertimbangkan konteks."""
    inputs = tokenizer(texts, return_tensors="pt", padding=True, truncation=True)
    translated = model.generate(**inputs)
    return [tokenizer.decode(t, skip_special_tokens=True) for t in translated]

def translate_srt(srt_content, batch_size=5):
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

def handler(event, context):
    """Handler untuk RunPod serverless dengan file upload (multipart support)."""
    try:
        # Dapatkan isi file dari event payload
        if "file_content" not in event:
            return {"statusCode": 400, "body": json.dumps({"error": "File content is required."})}

        # Decode base64 content (karena file upload biasanya dikodekan sebagai base64)
        srt_content = base64.b64decode(event["file_content"]).decode("utf-8")

        # Terjemahkan SRT
        translated_srt = translate_srt(srt_content)

        # Tulis hasil terjemahan ke file sementara
        output_path = "/tmp/translated_subtitle.srt"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(translated_srt)

        # Baca file hasil terjemahan untuk dikembalikan dalam respons
        with open(output_path, "r", encoding="utf-8") as f:
            translated_srt_content = f.read()

        # Return the translated file content for download
        return {
            "statusCode": 200,
            "headers": {
                # "Content-Disposition": "attachment; filename=translated_subtitle.srt",
                "Content-Type": "text/plain"
            },
            "body": {
                "file_content": base64.b64encode(translated_srt_content.encode("utf-8"))
            }
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }
