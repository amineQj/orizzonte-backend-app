"""Persistent EasyOCR server used by backend_bridge.py.

Run this in the isolated EasyOCR environment, not in the Django or
verification_agent virtual environments:

    C:\orizzonte_easyocr_env\Scripts\python.exe easyocr_server.py

The EasyOCR reader is created once at startup and reused for every request,
eliminating the previous per-photo subprocess cold start.
"""

from __future__ import annotations

import os
from typing import Any

import easyocr
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

HOST = os.environ.get("EASYOCR_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("EASYOCR_SERVER_PORT", "8001"))

reader = easyocr.Reader(["ar", "en"], gpu=False)

app = FastAPI(title="ORizzonte EasyOCR", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ocr")
async def ocr(request: Request) -> dict[str, Any]:
    image_bytes = await request.body()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty image")

    try:
        lines = reader.readtext(image_bytes, detail=0)
    except Exception as exc:  # EasyOCR failures must not crash the server.
        return JSONResponse(
            status_code=500,
            content={"status": "FAILED", "text": "", "error": str(exc)},
        )

    text = "\n".join(str(line) for line in lines).strip()
    if not text:
        return {"status": "EMPTY", "text": ""}
    return {"status": "USED", "text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")