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
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from faster_whisper import WhisperModel


# -------------------------------------------------
# App config
# -------------------------------------------------

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# Whisper
# "base" is fast. "small" is noticeably more accurate on many voices.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

# Whisper inference tuning (accuracy vs speed)
# If CPU struggles, set WHISPER_BEAM=1 and WHISPER_BEST_OF=1
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "2"))
WHISPER_BEST_OF = int(os.environ.get("WHISPER_BEST_OF", "2"))

# Target SR for Whisper
WHISPER_SR = 16000

# System audio cadence
# We transcribe a *window* of audio every *stride* seconds.
SYS_STRIDE_SECONDS = float(os.environ.get("SYS_STRIDE_SECONDS", "1.0"))  # update frequency
SYS_WINDOW_SECONDS = float(os.environ.get("SYS_WINDOW_SECONDS", "3.0"))  # context for accuracy

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

# Optional: pin a system audio device by index from sd.query_devices()
FORCED_SYSTEM_AUDIO_DEVICE = os.environ.get("SYSTEM_AUDIO_DEVICE_INDEX")
FORCED_SYSTEM_AUDIO_DEVICE = int(FORCED_SYSTEM_AUDIO_DEVICE) if FORCED_SYSTEM_AUDIO_DEVICE else None


# -------------------------------------------------
# Paths / static web
# -------------------------------------------------

def resource_dir(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


WEB_DIR = resource_dir("web")
WEB_DIR.mkdir(parents=True, exist_ok=True)


app = FastAPI(title="Watch & See Local Agent (Mic + System Audio)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(str(WEB_DIR / "manifest.webmanifest"))


@app.get("/sw.js")
def sw():
    return FileResponse(str(WEB_DIR / "sw.js"))


# -------------------------------------------------
# Models
# -------------------------------------------------

whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)


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


def merge_transcript(prev: str, new: str) -> str:
    """Best-effort de-dup when we transcribe overlapping windows."""
    prev = (prev or "").strip()
    new = (new or "").strip()
    if not new:
        return prev
    if not prev:
        return new

    if new.startswith(prev):
        return new
    if prev.endswith(new):
        return prev

    max_k = min(len(prev), len(new), 120)
    overlap = 0
    for k in range(1, max_k + 1):
        if prev[-k:] == new[:k]:
            overlap = k
    if overlap > 0:
        return (prev + new[overlap:]).strip()

    return (prev + " " + new).strip()


# -------------------------------------------------
# Audio device helpers
# -------------------------------------------------

def list_audio_devices() -> List[Dict[str, Any]]:
    devices = sd.query_devices()
    out: List[Dict[str, Any]] = []
    for idx, d in enumerate(devices):
        try:
            ha = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            ha = "?"
        out.append(
            {
                "index": idx,
                "name": str(d.get("name", "")),
                "hostapi": ha,
                "max_input_channels": int(d.get("max_input_channels", 0)),
                "max_output_channels": int(d.get("max_output_channels", 0)),
                "default_samplerate": float(d.get("default_samplerate", 0) or 0),
            }
        )
    return out


def is_hostapi(d: Dict[str, Any], contains: str) -> bool:
    try:
        return contains.lower() in sd.query_hostapis(d["hostapi"])["name"].lower()
    except Exception:
        return False


def pick_system_audio_device(forced_index: Optional[int]) -> int:
    devices = sd.query_devices()

    if forced_index is not None:
        d = devices[forced_index]
        if is_hostapi(d, "WDM-KS"):
            raise RuntimeError(
                "Selected device uses WDM-KS and often fails. Pick Stereo Mix on WASAPI/DirectSound/MME instead."
            )
        return int(forced_index)

    # Prefer WASAPI Stereo Mix
    for idx, d in enumerate(devices):
        name = str(d.get("name", "")).lower()
        if "stereo mix" in name and d.get("max_input_channels", 0) > 0 and is_hostapi(d, "WASAPI"):
            return idx

    # Then DirectSound Stereo Mix
    for idx, d in enumerate(devices):
        name = str(d.get("name", "")).lower()
        if "stereo mix" in name and d.get("max_input_channels", 0) > 0 and is_hostapi(d, "DirectSound"):
            return idx

    # Then MME Stereo Mix
    for idx, d in enumerate(devices):
        name = str(d.get("name", "")).lower()
        if "stereo mix" in name and d.get("max_input_channels", 0) > 0 and is_hostapi(d, "MME"):
            return idx

    # Fallback: WASAPI loopback devices
    for idx, d in enumerate(devices):
        name = str(d.get("name", "")).lower()
        if "loopback" in name and d.get("max_input_channels", 0) > 0 and is_hostapi(d, "WASAPI"):
            return idx

    raise RuntimeError(
        "No system-audio capture device found.\n"
        "Fix: enable Stereo Mix in Windows:\n"
        "  Control Panel → Sound → Recording → right click → Show Disabled Devices → Enable Stereo Mix\n"
        "Then restart this app."
    )


# -------------------------------------------------
# System audio manager
# -------------------------------------------------

class SystemAudioManager:
    """Captures system output (Stereo Mix / loopback), transcribes continuously."""

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

        self.device_index: Optional[int] = None
        self.device_name: Optional[str] = None
        self.device_hostapi: Optional[str] = None
        self.capture_sr: Optional[int] = None
        self.channels: Optional[int] = None

        self.forced_index: Optional[int] = FORCED_SYSTEM_AUDIO_DEVICE

    def set_device_index(self, idx: Optional[int]):
        with self.lock:
            self.forced_index = idx

    def _append_audio(self, indata: np.ndarray):
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = np.mean(indata, axis=1).astype(np.float32)
        else:
            mono = indata.reshape(-1).astype(np.float32)
        self.buffer.append(mono)
        self.buffer_samples += mono.shape[0]

        # Keep last ~60s so memory doesn't grow forever
        max_samples = int((self.capture_sr or 48000) * 60)
        while self.buffer_samples > max_samples and self.buffer:
            a = self.buffer.popleft()
            self.buffer_samples -= a.shape[0]

    def _get_last_n_samples(self, n_samples: int) -> Optional[np.ndarray]:
        if self.buffer_samples < n_samples:
            return None
        parts = []
        remaining = n_samples
        for a in reversed(self.buffer):
            if remaining <= 0:
                break
            take = min(a.shape[0], remaining)
            parts.append(a[-take:])
            remaining -= take
        if remaining > 0:
            return None
        return np.concatenate(list(reversed(parts)))

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

    def _transcribe_float(self, pcm_float: np.ndarray, src_sr: int) -> str:
        pcm_16k = resample_linear(pcm_float, src_sr=src_sr, dst_sr=WHISPER_SR)
        pcm_int16 = np.clip(pcm_16k * 32767.0, -32768, 32767).astype(np.int16)
        wav_path = self._write_wav(pcm_int16, WHISPER_SR)
        try:
            segments, _info = whisper.transcribe(
                wav_path,
                vad_filter=True,
                beam_size=WHISPER_BEAM,
                best_of=WHISPER_BEST_OF,
                temperature=0.0,
                condition_on_previous_text=False,
            )
            return " ".join([s.text.strip() for s in segments if s.text]).strip()
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass

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
                "channels": self.channels,
                "forced_index": self.forced_index,
                "sys_stride_seconds": SYS_STRIDE_SECONDS,
                "sys_window_seconds": SYS_WINDOW_SECONDS,
            }

    def latest(self) -> Dict[str, Any]:
        with self.lock:
            return {"text": self.latest_text, "ts": self.latest_ts, "error": self.error}

    def _run(self):
        try:
            idx = pick_system_audio_device(self.forced_index)
            d = sd.query_devices(idx)
            capture_sr = int(d.get("default_samplerate") or 48000)
            channels = max(1, min(2, int(d.get("max_input_channels") or 1)))

            with self.lock:
                self.device_index = int(idx)
                self.device_name = str(d.get("name", ""))
                try:
                    self.device_hostapi = sd.query_hostapis(d["hostapi"])["name"]
                except Exception:
                    self.device_hostapi = None
                self.capture_sr = capture_sr
                self.channels = channels

            def callback(indata, frames, t, status):
                if status:
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
                device=idx,
                blocksize=0,
            )
            self.stream.start()

            window_samples = int(capture_sr * max(0.5, SYS_WINDOW_SECONDS))
            stride_sleep = max(0.2, SYS_STRIDE_SECONDS)
            last_tick = 0.0

            while True:
                with self.lock:
                    if not self.running:
                        break

                now = time.time()
                if (now - last_tick) < stride_sleep:
                    time.sleep(0.05)
                    continue
                last_tick = now

                chunk = self._get_last_n_samples(window_samples)
                if chunk is None:
                    continue

                try:
                    text = self._transcribe_float(chunk, src_sr=capture_sr)
                except Exception as e:
                    with self.lock:
                        self.error = f"Transcribe error: {e}"
                    continue

                if text:
                    with self.lock:
                        self.latest_text = merge_transcript(self.latest_text, text)
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


# -------------------------------------------------
# API
# -------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "whisper_model": WHISPER_MODEL,
        "whisper_device": WHISPER_DEVICE,
        "whisper_compute": WHISPER_COMPUTE,
        "whisper_beam": WHISPER_BEAM,
        "whisper_best_of": WHISPER_BEST_OF,
        "sys_stride_seconds": SYS_STRIDE_SECONDS,
        "sys_window_seconds": SYS_WINDOW_SECONDS,
        "ollama_ok": ollama_ok(),
        "ollama_model": OLLAMA_MODEL,
        "system_audio": system_audio.status(),
        "time": time.time(),
    }


@app.get("/api/audio_devices")
def audio_devices():
    return {"devices": list_audio_devices()}


class SelectDeviceReq(BaseModel):
    index: Optional[int] = None


@app.post("/api/system_audio/select")
def system_audio_select(req: SelectDeviceReq):
    was_running = system_audio.status().get("running", False)
    if was_running:
        system_audio.stop()
        time.sleep(0.15)
    system_audio.set_device_index(req.index)
    if was_running:
        system_audio.start()
    return system_audio.status()


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
    data = await audio.read()
    if not data:
        return {"text": ""}

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        segments, info = whisper.transcribe(
            tmp_path,
            vad_filter=True,
            beam_size=WHISPER_BEAM,
            best_of=WHISPER_BEST_OF,
            temperature=0.0,
            condition_on_previous_text=False,
        )
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
You are a real-time call coach. Based ONLY on the transcript snippet, give actionable tips.

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
        raw = (data.get("response") or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return None
        obj = json.loads(raw[start : end + 1])
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