import base64

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from translate import translate_srt

app = FastAPI(title="transub", version="1.0")


class TranslateRequest(BaseModel):
    srt_text_base64: str


class TranslateResponse(BaseModel):
    translated_srt_base64: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    if not req.srt_text_base64:
        raise HTTPException(status_code=400, detail="srt_text_base64 is required.")

    try:
        srt_content = base64.b64decode(req.srt_text_base64).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decode base64 SRT: {exc}")

    try:
        # translate_srt bersifat blocking (panggilan HTTP + threadpool internal),
        # jalankan di threadpool agar event loop tidak terblok.
        translated_srt = await run_in_threadpool(translate_srt, srt_content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Translation failed: {exc}")

    translated_srt_base64 = base64.b64encode(
        translated_srt.encode("utf-8")
    ).decode("utf-8")
    return TranslateResponse(translated_srt_base64=translated_srt_base64)
