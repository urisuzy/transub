from translate import handler

import base64

# baca file test/sub.srt
with open("test/sub.srt", "r") as file:
    srt_content = file.read()

# ubah jadi base64
srt_text_base64 = base64.b64encode(srt_content.encode("utf-8")).decode("utf-8")

# buat payload
event = {
    "input": {
        "srt_text_base64": srt_text_base64
    }
}

# panggil handler
result = handler(event)

# print hasil
print(result)

# decode base64
result_text = base64.b64decode(result["translated_srt_base64"]).decode("utf-8")

# tulis ke file test/sub_translated.srt
with open("test/sub_translated.srt", "w") as file:
    file.write(result_text)