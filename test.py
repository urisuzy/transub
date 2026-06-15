from translate import handler

import base64
import datetime

# baca file test/sub.srt
with open("test/sub.srt", "r", encoding="utf-8") as file:
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

# cek error sebelum akses hasil
if "error" in result:
    raise SystemExit(f"Handler error: {result['error']}")

# decode base64
result_text = base64.b64decode(result["translated_srt_base64"]).decode("utf-8")

# tulis ke file dengan timestamp di belakang nama, biar bisa multiple test
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
output_path = f"test/sub_translated_{timestamp}.srt"
with open(output_path, "w", encoding="utf-8") as file:
    file.write(result_text)

print(f"Done -> {output_path}")