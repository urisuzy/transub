import base64

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from translate import translate_subtitle

app = FastAPI(title="transub", version="1.0")


class TranslateRequest(BaseModel):
    srt_text_base64: str


class TranslateResponse(BaseModel):
    translated_srt_base64: str
    token_usage: dict | None = None
    cached: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    if not req.srt_text_base64:
        raise HTTPException(
            status_code=400,
            detail="srt_text_base64 is required.",
        )
    try:
        subtitle_content = base64.b64decode(
            req.srt_text_base64
        ).decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to decode base64 subtitle: {exc}",
        )
    try:
        result = await run_in_threadpool(
            translate_subtitle,
            subtitle_content,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Translation failed: {exc}",
        )

    translated_base64 = base64.b64encode(
        result["srt"].encode("utf-8")
    ).decode("utf-8")
    return TranslateResponse(
        translated_srt_base64=translated_base64,
        token_usage=result.get("token_usage"),
        cached=result.get("cached", False),
    )
