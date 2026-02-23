import json
import os
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import requests
import sounddevice as sd
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from faster_whisper import WhisperModel

APP_HOST = "127.0.0.1"
APP_PORT = 8000

# ------------------ Settings ------------------ #
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")   # base=fast
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "int8")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

WHISPER_SR = 16000
CHUNK_SECONDS = float(os.environ.get("CHUNK_SECONDS", "2.0"))

# Force device index if you want (ex: 15 from your dump)
FORCED_SYSTEM_AUDIO_DEVICE = os.environ.get("SYSTEM_AUDIO_DEVICE_INDEX")
FORCED_SYSTEM_AUDIO_DEVICE = int(FORCED_SYSTEM_AUDIO_DEVICE) if FORCED_SYSTEM_AUDIO_DEVICE else None

# ------------------ Helpers ------------------ #
def resource_dir(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative

WEB_DIR = resource_dir("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Watch & See Local App (Mic + System Audio)")

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

def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Simple linear resampler (no extra deps)."""
    if src_sr == dst_sr:
        return x.astype(np.float32, copy=False)
    if x.size == 0:
        return x.astype(np.float32)
    duration = x.shape[0] / float(src_sr)
    dst_len = int(duration * dst_sr)
    if dst_len <= 1:
        return x[:1].astype(np.float32)
    src_t = np.linspace(0.0, duration, num=x.shape[0], endpoint=False)
    dst_t = np.linspace(0.0, duration, num=dst_len, endpoint=False)
    return np.interp(dst_t, src_t, x).astype(np.float32)

def list_audio_devices() -> List[Dict[str, Any]]:
    devices = sd.query_devices()
    out: List[Dict[str, Any]] = []
    for i, d in enumerate(devices):
        hostapi_name = ""
        try:
            hostapi_name = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            pass
        out.append({
            "index": i,
            "name": str(d.get("name", "")),
            "hostapi": hostapi_name,
            "max_input_channels": int(d.get("max_input_channels") or 0),
            "max_output_channels": int(d.get("max_output_channels") or 0),
            "default_samplerate": float(d.get("default_samplerate") or 0.0),
        })
    return out

# ------------------ System Audio Capture ------------------ #
class SystemAudioManager:
    """
    Captures system audio primarily via Stereo Mix.
    - Avoids WDM-KS (often crashes with -9999).
    - Uses device default sample rate (fixes -9997 invalid sample rate).
    - Provides RMS debug so we can tell if audio is flowing.
    """

    def __init__(self):
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        self.latest_text: str = ""
        self.latest_ts: float = 0.0
        self.error: Optional[str] = None

        self.buffer: Deque[np.ndarray] = deque()
        self.buffer_samples: int = 0

        self.stream: Optional[sd.InputStream] = None

        # debug / info
        self.device_index: Optional[int] = None
        self.device_name: Optional[str] = None
        self.device_hostapi: Optional[str] = None
        self.capture_sr: Optional[int] = None
        self.rms: float = 0.0  # audio level debug

    def _hostapi_name(self, d: Dict[str, Any]) -> str:
        try:
            return str(sd.query_hostapis(d["hostapi"])["name"])
        except Exception:
            return ""

    def _set_selected(self, idx: int, d: Dict[str, Any]) -> int:
        self.device_index = idx
        self.device_name = str(d.get("name", ""))
        self.device_hostapi = self._hostapi_name(d)
        try:
            self.capture_sr = int(d.get("default_samplerate") or 48000)
        except Exception:
            self.capture_sr = 48000
        return idx

    def _is_hostapi(self, d: Dict[str, Any], contains: str) -> bool:
        return contains.lower() in self._hostapi_name(d).lower()

    def _is_wdmks(self, d: Dict[str, Any]) -> bool:
        return self._is_hostapi(d, "WDM-KS")

    def _get_system_audio_device(self) -> int:
        devices = sd.query_devices()

        # If user forced a device index, use it (but reject WDM-KS)
        if FORCED_SYSTEM_AUDIO_DEVICE is not None:
            d = devices[FORCED_SYSTEM_AUDIO_DEVICE]
            if self._is_wdmks(d):
                raise RuntimeError("Forced device is WDM-KS (unstable). Pick WASAPI/MME/DirectSound instead.")
            return self._set_selected(FORCED_SYSTEM_AUDIO_DEVICE, d)

        # Prefer Stereo Mix: WASAPI -> DirectSound -> MME
        # (avoid WDM-KS due to -9999 / driver ioctl errors)
        priorities = ["WASAPI", "DirectSound", "MME"]
        for backend in priorities:
            for idx, d in enumerate(devices):
                name = str(d.get("name", "")).lower()
                if "stereo mix" in name and int(d.get("max_input_channels") or 0) > 0:
                    if self._is_hostapi(d, backend) and not self._is_wdmks(d):
                        return self._set_selected(idx, d)

        raise RuntimeError(
            "No usable Stereo Mix found (WASAPI/DirectSound/MME).\n"
            "Fix: Control Panel → Sound → Recording → right click → Show Disabled Devices → Enable Stereo Mix.\n"
            "Also ensure your output device is set correctly (Speakers/Headphones)."
        )

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.latest_text = ""
            self.latest_ts = 0.0
            self.error = None
            self.buffer.clear()
            self.buffer_samples = 0
            self.rms = 0.0

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        with self.lock:
            self.running = False
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "latest_ts": self.latest_ts,
                "latest_text": self.latest_text,
                "error": self.error,
                "device_index": self.device_index,
                "device_name": self.device_name,
                "device_hostapi": self.device_hostapi,
                "capture_sr": self.capture_sr,
                "whisper_sr": WHISPER_SR,
                "rms": float(self.rms),
            }

    def latest(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "text": self.latest_text,
                "ts": self.latest_ts,
                "error": self.error,
                "rms": float(self.rms),
            }

    def _append_audio(self, indata: np.ndarray):
        # Convert to mono float32
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = np.mean(indata, axis=1).astype(np.float32)
        else:
            mono = indata.reshape(-1).astype(np.float32)

        # audio level debug (RMS)
        if mono.size:
            self.rms = float(np.sqrt(np.mean(mono * mono)))
        else:
            self.rms = 0.0

        self.buffer.append(mono)
        self.buffer_samples += mono.shape[0]

    def _pop_chunk(self, n_samples: int) -> Optional[np.ndarray]:
        if self.buffer_samples < n_samples:
            return None
        parts = []
        remaining = n_samples
        while remaining > 0 and self.buffer:
            a = self.buffer[0]
            if a.shape[0] <= remaining:
                parts.append(a)
                remaining -= a.shape[0]
                self.buffer.popleft()
            else:
                parts.append(a[:remaining])
                self.buffer[0] = a[remaining:]
                remaining = 0
        self.buffer_samples -= n_samples
        return np.concatenate(parts) if parts else None

    @staticmethod
    def _write_wav(pcm_int16: np.ndarray, sr: int) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm_int16.tobytes())
        return path

    def _transcribe_pcm(self, pcm_float: np.ndarray, src_sr: int) -> str:
        pcm_16k = resample_linear(pcm_float, src_sr=src_sr, dst_sr=WHISPER_SR)
        pcm_int16 = np.clip(pcm_16k * 32767.0, -32768, 32767).astype(np.int16)
        wav_path = self._write_wav(pcm_int16, WHISPER_SR)
        try:
            segments, _info = whisper.transcribe(wav_path, vad_filter=True, beam_size=1)
            return " ".join([s.text.strip() for s in segments if s.text]).strip()
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass

    def _run(self):
        try:
            device_index = self._get_system_audio_device()
            d = sd.query_devices(device_index)

            # IMPORTANT: use device default samplerate (avoids -9997)
            capture_sr = int(d.get("default_samplerate") or 48000)

            # Force mono for stability (Stereo Mix often weird in stereo)
            channels = 1

            with self.lock:
                self.capture_sr = capture_sr

            def callback(indata, frames, t, status):
                if status:
                    # keep running, but could log if desired
                    pass
                with self.lock:
                    if not self.running:
                        return
                self._append_audio(indata.copy())

            self.stream = sd.InputStream(
                samplerate=capture_sr,
                channels=channels,
                dtype="float32",
                callback=callback,
                device=device_index,
                blocksize=0,
            )
            self.stream.start()

            n_samples = int(capture_sr * CHUNK_SECONDS)

            while True:
                with self.lock:
                    if not self.running:
                        break

                chunk = self._pop_chunk(n_samples)
                if chunk is None:
                    time.sleep(0.1)
                    continue

                # If audio is basically silent, skip to avoid useless transcribes
                if float(self.rms) < 0.0008:
                    continue

                try:
                    text = self._transcribe_pcm(chunk, src_sr=capture_sr)
                except Exception as e:
                    with self.lock:
                        self.error = f"Transcribe error: {e}"
                    continue

                if text:
                    with self.lock:
                        self.latest_text = (self.latest_text + " " + text).strip()
                        self.latest_ts = time.time()

        except Exception as e:
            with self.lock:
                self.error = str(e)
                self.running = False
        finally:
            try:
                if self.stream is not None:
                    self.stream.stop()
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            with self.lock:
                self.running = False

system_audio = SystemAudioManager()

# ------------------ API ------------------ #
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "whisper_model": WHISPER_MODEL,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "ollama_ok": ollama_ok(),
        "ollama_model": OLLAMA_MODEL,
        "system_audio": system_audio.status(),
        "time": time.time(),
    }

@app.get("/api/system_audio/devices")
def system_audio_devices():
    return {"devices": list_audio_devices()}

@app.post("/api/system_audio/start")
def system_audio_start():
    system_audio.start()
    return system_audio.status()

@app.post("/api/system_audio/stop")
def system_audio_stop():
    system_audio.stop()
    return system_audio.status()

@app.get("/api/system_audio/status")
def system_audio_status():
    return system_audio.status()

@app.get("/api/system_audio/latest")
def system_audio_latest():
    return system_audio.latest()

@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Mic chunk transcription (webm from browser)."""
    data = await audio.read()
    if not data:
        return {"text": ""}

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        segments, info = whisper.transcribe(tmp_path, vad_filter=True)
        text = " ".join([s.text.strip() for s in segments if s.text]).strip()
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

    filler_words = [" um ", " uh ", " like "]
    filler_count = sum(t.count(w) for w in filler_words)
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
        if start == -1 or end == -1:
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