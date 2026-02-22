import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from faster_whisper import WhisperModel

APP_HOST = "127.0.0.1"
APP_PORT = 8000

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "int8")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def resource_dir(relative: str) -> Path:
    # Works in dev + PyInstaller onefile
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


WEB_DIR = resource_dir("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Watch & See Local App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve UI
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


@app.get("/")
def root():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/app.js")
def app_js():
    return FileResponse(str(WEB_DIR / "app.js"))


@app.get("/style.css")
def style_css():
    return FileResponse(str(WEB_DIR / "style.css"))


# Load whisper once
whisper = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)


def ollama_ok() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "whisper_model": WHISPER_MODEL,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "ollama_ok": ollama_ok(),
        "ollama_model": OLLAMA_MODEL,
        "time": time.time(),
    }


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        return {"text": ""}

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        segments, info = whisper.transcribe(tmp_path, vad_filter=True)
        text_parts: List[str] = []
        for s in segments:
            if s.text:
                text_parts.append(s.text.strip())
        text = " ".join(text_parts).strip()
        return {"text": text, "language": getattr(info, "language", None)}
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


class CoachReq(BaseModel):
    transcript: str


def rule_coach(text: str) -> Dict[str, Any]:
    t = (text or "").lower()
    tips: List[str] = []

    empathy_phrases = ["i understand", "that makes sense", "sorry", "i can imagine", "i hear you"]
    has_empathy = any(p in t for p in empathy_phrases)
    if not has_empathy and len(text) > 120:
        tips.append('Add empathy: try “I understand” or “That makes sense.”')

    qmarks = text.count("?")
    if len(text) > 160 and qmarks == 0:
        tips.append("Ask a clarifying question to confirm details.")

    next_signals = ["next step", "i will", "we will", "you will receive", "timeline", "within", "by end of day"]
    has_next = any(p in t for p in next_signals)
    if not has_next and len(text) > 180:
        tips.append("Confirm next steps + timeline (what will happen and by when).")

    if "cancel" in t or "refund" in t or "charged" in t:
        tips.append("Acknowledge the issue, confirm policy, and restate resolution + timeframe.")

    filler_count = sum(t.count(w) for w in [" um ", " uh ", " like "])
    if filler_count >= 3:
        tips.append("Reduce filler words (pause instead).")

    if not tips:
        tips.append("Good pace. Keep summarizing and confirming next steps.")

    scorecard = {
        "clarity": 8 if qmarks > 0 else 6,
        "empathy": 8 if has_empathy else 4,
        "next_steps": 8 if has_next else 4,
        "compliance": 7,
    }
    return {"tips": tips, "scorecard": scorecard, "source": "rules"}


def call_ollama(transcript: str) -> Optional[Dict[str, Any]]:
    prompt = f"""
You are a real-time call coach (Observe-style). Based ONLY on the transcript snippet, give actionable tips.

Transcript snippet:
{transcript}

Return STRICT JSON only with:
{{
  "tips": ["...", "..."],
  "scorecard": {{
    "clarity": 0-10,
    "empathy": 0-10,
    "next_steps": 0-10,
    "compliance": 0-10
  }}
}}

Rules:
- tips: 1 to 4 items, short, actionable
- If nothing bad, give performance reinforcement + next best step
"""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }

    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=12)
        r.raise_for_status()
        data = r.json()
        raw = data.get("response", "").strip()

        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        obj = json.loads(raw[start:end + 1])
        if "tips" not in obj:
            return None

        obj["source"] = "ollama"
        return obj
    except Exception:
        return None


@app.post("/api/coach")
def coach(req: CoachReq):
    snippet = (req.transcript or "").strip()
    if len(snippet) < 10:
        return {"tips": ["Start speaking and I’ll coach in real-time."], "scorecard": {}, "source": "none"}

    if ollama_ok():
        res = call_ollama(snippet[-1200:])
        if res:
            return res

    return rule_coach(snippet[-1200:])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)