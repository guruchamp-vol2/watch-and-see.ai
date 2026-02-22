# Watch & See AI (Live Coach) - $0 Budget

This is a live coaching web app (Observe-style) that:
- Listens to microphone audio in real time
- Shows live transcript
- Gives coaching tips continuously

It supports 2 modes:

## Mode A: Browser Speech (no install)
Uses the browser's SpeechRecognition (Chrome/Edge best).
Pros: easiest for students, live, no uploads
Cons: quality varies, not supported everywhere

## Mode B: Local Whisper + Ollama (best quality, still $0)
Students run a tiny local helper API on their computer:
- faster-whisper for transcription (audio stays on their device)
- optional Ollama for smarter tips (still local)

The online web app calls: http://127.0.0.1:8000 (localhost)

---

# 1) Run the web app
Open `frontend/index.html` directly OR host `frontend/` on any static host (GitHub Pages, Vercel static, etc.).

---

# 2) (Optional) Run the Local Agent for best quality
## Windows (PowerShell)
cd local_agent
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py

Then open the web app and choose: "Local Whisper + Ollama" mode.

---

# 3) (Optional) Enable smarter tips with Ollama
Install Ollama and pull a model:
ollama pull llama3.1:8b

Make sure Ollama is running:
ollama serve

The Local Agent will automatically use Ollama if available; otherwise it falls back to rule-based tips.