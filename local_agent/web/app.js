const BASE = window.location.origin;

const elMode = document.getElementById("mode");
const elStart = document.getElementById("btnStart");
const elStop = document.getElementById("btnStop");
const elStatus = document.getElementById("status");
const elTranscript = document.getElementById("transcript");
const elTips = document.getElementById("tips");
const elHealth = document.getElementById("btnHealth");
const elClear = document.getElementById("btnClear");
const elCopy = document.getElementById("btnCopy");

const sClarity = document.getElementById("sClarity");
const sEmpathy = document.getElementById("sEmpathy");
const sNext = document.getElementById("sNext");
const sCompliance = document.getElementById("sCompliance");

let running = false;
let transcriptText = "";
let tipTimer = null;
let systemPollTimer = null;

// Browser SpeechRecognition
let recognition = null;

// Mic whisper recording
let mediaStream = null;
let recorder = null;
let chunkTimer = null;
let pendingChunk = false;

function setStatus(msg) { elStatus.textContent = msg; }

function addTranscript(text) {
  if (!text) return;
  transcriptText += (transcriptText ? " " : "") + text.trim();
  elTranscript.textContent = transcriptText;
  elTranscript.scrollTop = elTranscript.scrollHeight;
}

function setTips(tips) {
  elTips.innerHTML = "";
  (tips || []).slice(0, 6).forEach(t => {
    const li = document.createElement("li");
    li.textContent = t;
    elTips.appendChild(li);
  });
}

function setScorecard(sc = {}) {
  sClarity.textContent = sc.clarity ?? "—";
  sEmpathy.textContent = sc.empathy ?? "—";
  sNext.textContent = sc.next_steps ?? "—";
  sCompliance.textContent = sc.compliance ?? "—";
}

function ruleCoach(text) {
  const t = (text || "").toLowerCase();
  const tips = [];

  const empathyPhrases = ["i understand", "that makes sense", "sorry", "i can imagine", "i hear you"];
  const hasEmpathy = empathyPhrases.some(p => t.includes(p));
  if (!hasEmpathy && text.length > 120) tips.push("Add empathy: try “I understand” or “That makes sense.”");

  const questionMarks = (text.match(/\?/g) || []).length;
  if (text.length > 160 && questionMarks === 0) tips.push("Ask a clarifying question to confirm details.");

  const nextStepSignals = ["next step", "i will", "we will", "you will receive", "timeline", "within", "by end of day"];
  const hasNext = nextStepSignals.some(p => t.includes(p));
  if (!hasNext && text.length > 180) tips.push("Confirm next steps + timeline (what will happen and by when).");

  if (t.includes("cancel") || t.includes("refund") || t.includes("charged")) {
    tips.push("Acknowledge the issue, confirm policy, and restate resolution + timeframe.");
  }

  const fillerCount = (t.match(/\b(um+|uh+|like)\b/g) || []).length;
  if (fillerCount >= 4) tips.push("Reduce filler words (pause instead of “um/like”).");

  const score = {
    clarity: Math.min(10, Math.max(1, Math.round(6 + (questionMarks > 0 ? 1 : 0)))),
    empathy: hasEmpathy ? 8 : 4,
    next_steps: hasNext ? 8 : 4,
    compliance: 7,
  };

  if (tips.length === 0) tips.push("Good pace. Keep summarizing and confirming next steps.");
  return { tips, scorecard: score };
}

function getRecentText(maxChars = 900) {
  if (transcriptText.length <= maxChars) return transcriptText;
  return transcriptText.slice(transcriptText.length - maxChars);
}

async function agentHealth() {
  try {
    const r = await fetch(`${BASE}/api/health`);
    if (!r.ok) throw new Error("not ok");
    const j = await r.json();
    setStatus(`Local App OK • whisper=${j.whisper_model} • ollama=${j.ollama_ok ? "yes" : "no"}`);
  } catch {
    setStatus("Local App not reachable.");
  }
}

async function agentCoach(text) {
  try {
    const r = await fetch(`${BASE}/api/coach`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: text })
    });
    if (!r.ok) throw new Error("coach bad");
    return await r.json();
  } catch {
    return null;
  }
}

// ---- Mic chunk transcription ----
async function sendMicChunk(blob) {
  if (pendingChunk) return;
  pendingChunk = true;

  try {
    const fd = new FormData();
    fd.append("audio", blob, "chunk.webm");

    const r = await fetch(`${BASE}/api/transcribe`, { method: "POST", body: fd });
    if (!r.ok) throw new Error("transcribe bad");

    const j = await r.json();
    if (j.text) addTranscript(j.text);
  } catch {
    setStatus("Mic transcription failed (check terminal).");
  } finally {
    pendingChunk = false;
  }
}

function supportsBrowserSpeech() {
  return ("webkitSpeechRecognition" in window) || ("SpeechRecognition" in window);
}

function startBrowserSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { setStatus("Browser Speech not supported."); return; }

  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let finalBuffer = "";

  recognition.onstart = () => setStatus("Listening (Browser Speech)...");
  recognition.onerror = (e) => setStatus(`Speech error: ${e.error}`);
  recognition.onend = () => { if (running) { try { recognition.start(); } catch {} } };

  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      const text = r[0].transcript;
      if (r.isFinal) finalBuffer += " " + text;
    }
    if (finalBuffer.trim()) { addTranscript(finalBuffer.trim()); finalBuffer = ""; }
  };

  recognition.start();
}

async function startMicWhisper() {
  setStatus("Requesting microphone...");
  mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });

  recorder = new MediaRecorder(mediaStream, { mimeType: "audio/webm" });
  const chunks = [];

  recorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunks.push(e.data); };
  recorder.onstart = () => setStatus("Listening (Mic Whisper)...");
  recorder.onstop = async () => {
    if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
    setStatus("Stopped.");
  };

  recorder.start();
  chunkTimer = setInterval(async () => {
    if (!recorder || recorder.state !== "recording") return;
    recorder.requestData();
    if (chunks.length > 0) {
      const blob = new Blob(chunks.splice(0, chunks.length), { type: "audio/webm" });
      await sendMicChunk(blob);
    }
  }, 2000);
}

// ---- System audio mode ----
async function startSystemAudio() {
  setStatus("Starting System Audio (Windows loopback)...");
  const r = await fetch(`${BASE}/api/system_audio/start`, { method: "POST" });
  const j = await r.json();
  if (j.error) {
    setStatus(`System audio error: ${j.error}`);
    throw new Error(j.error);
  }
  setStatus("Listening (System Audio)...");
  // poll latest transcript
  systemPollTimer = setInterval(async () => {
    const rr = await fetch(`${BASE}/api/system_audio/latest`);
    const jj = await rr.json();
    if (jj.error) {
      setStatus(`System audio error: ${jj.error}`);
      return;
    }
    if (jj.text && jj.text.length > transcriptText.length) {
      // naive: replace with latest full transcript
      transcriptText = jj.text;
      elTranscript.textContent = transcriptText;
      elTranscript.scrollTop = elTranscript.scrollHeight;
    }
  }, 1000);
}

async function stopSystemAudio() {
  try { await fetch(`${BASE}/api/system_audio/stop`, { method: "POST" }); } catch {}
  if (systemPollTimer) clearInterval(systemPollTimer);
  systemPollTimer = null;
}

async function startCoachLoop() {
  tipTimer = setInterval(async () => {
    const recent = getRecentText(900);
    if (!recent || recent.trim().length < 20) return;

    const res = await agentCoach(recent);
    if (res && res.tips) {
      setTips(res.tips);
      setScorecard(res.scorecard || {});
      return;
    }
    const fallback = ruleCoach(recent);
    setTips(fallback.tips);
    setScorecard(fallback.scorecard);
  }, 3000);
}

function stopAll() {
  running = false;
  elStart.disabled = false;
  elStop.disabled = true;

  if (tipTimer) clearInterval(tipTimer);
  tipTimer = null;

  if (recognition) { try { recognition.onend = null; recognition.stop(); } catch {} recognition = null; }

  if (chunkTimer) clearInterval(chunkTimer);
  chunkTimer = null;

  if (recorder && recorder.state === "recording") { try { recorder.stop(); } catch {} }
  recorder = null;

  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }

  stopSystemAudio();

  setStatus("Stopped.");
}

elHealth.addEventListener("click", agentHealth);

elStart.addEventListener("click", async () => {
  elStart.disabled = true;
  elStop.disabled = false;
  running = true;

  await startCoachLoop();

  try {
    if (elMode.value === "browser") {
      if (!supportsBrowserSpeech()) { setStatus("Browser Speech not supported."); return; }
      startBrowserSpeech();
    } else if (elMode.value === "local") {
      await agentHealth();
      await startMicWhisper();
    } else if (elMode.value === "system") {
      await agentHealth();
      await startSystemAudio();
    }
  } catch (e) {
    stopAll();
  }
});

elStop.addEventListener("click", stopAll);

elClear.addEventListener("click", () => {
  transcriptText = "";
  elTranscript.textContent = "";
  setTips([]);
  setScorecard({});
});

elCopy.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(transcriptText || ""); setStatus("Transcript copied."); }
  catch { setStatus("Copy failed."); }
});

setTips(["Click “Start Listening” to begin."]);
setScorecard({});