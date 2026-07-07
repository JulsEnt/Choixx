from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from song2choir_engine import RenderOptions, render_from_upload_bytes

APP_NAME = "Song2Choir Pro"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "35"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title=APP_NAME, version="1.1.0")

origins_raw = os.getenv("CORS_ORIGINS", "*")
origins = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "X-S2C-Style",
        "X-S2C-Tempo",
        "X-S2C-Key",
        "X-S2C-Duration",
        "X-S2C-Engine",
    ],
)


@app.get("/api")
def api_root():
    return {
        "name": APP_NAME,
        "status": "online",
        "docs": "/docs",
        "health": "/api/health",
        "render_endpoint": "/api/render",
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "max_upload_mb": MAX_UPLOAD_MB,
        "allowed_audio": sorted(ALLOWED_EXTENSIONS),
    }


def _clean_filename(name: str | None) -> str:
    fallback = "upload.wav"
    if not name:
        return fallback
    return Path(name).name.replace("\x00", "") or fallback


@app.post("/api/render")
async def render(
    file: Annotated[UploadFile, File(description="Audio file to convert")],
    style: Annotated[str, Form()] = "gospel",
    intensity: Annotated[float, Form()] = 0.72,
    room: Annotated[float, Form()] = 0.62,
    warmth: Annotated[float, Form()] = 0.58,
    harmony: Annotated[str, Form()] = "gospel_stack",
    keep_original: Annotated[float, Form()] = 0.28,
):
    filename = _clean_filename(file.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio type. Use one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Upload an audio file first.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large. Max upload is {MAX_UPLOAD_MB}MB.")

    options = RenderOptions(
        style=style,
        intensity=intensity,
        room=room,
        warmth=warmth,
        harmony=harmony,
        keep_original=keep_original,
    )
    try:
        audio_bytes, meta = render_from_upload_bytes(data, suffix=suffix, options=options)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not render this audio: {exc}") from exc

    base = Path(filename).stem[:70] or "song"
    headers = {
        "Content-Disposition": f'attachment; filename="{base}-song2choir-pro.wav"',
        "X-S2C-Style": str(meta.get("style", "")),
        "X-S2C-Tempo": str(meta.get("tempo_bpm", "")),
        "X-S2C-Key": str(meta.get("estimated_key", "")),
        "X-S2C-Duration": str(meta.get("duration_seconds", "")),
        "X-S2C-Engine": str(meta.get("engine", "")),
    }
    return Response(content=audio_bytes, media_type="audio/wav", headers=headers)


@app.get("/", include_in_schema=False)
def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Frontend files are missing from the static folder.")
    return FileResponse(index_path)


# Serve style.css, app.js, images, and any future frontend assets.
# API routes are defined before this mount, so /api/* keeps working.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
