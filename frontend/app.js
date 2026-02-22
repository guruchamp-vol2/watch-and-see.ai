// Hosted (GitHub Pages) version:
// - Browser Speech works online
// - Pro mode (Local Whisper) requires running the Local App on http://127.0.0.1:8000

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

// Browser SpeechRecognition
let recognition = null;

function setStatus(msg) {
  elStatus.textContent = msg;
}

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

/** ----------------- Rule-based coach ----------------- **/
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

/** ----------------- Browser Speech mode ----------------- **/
function supportsBrowserSpeech() {
  return ("webkitSpeechRecognition" in window) || ("SpeechRecognition" in window);
}

function startBrowserSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    setStatus("Browser Speech not supported. Use Chrome/Edge, or download Pro Mode.");
    return;
  }

  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let finalBuffer = "";

  recognition.onstart = () => setStatus("Listening (Browser Speech)...");
  recognition.onerror = (e) => setStatus(`Speech error: ${e.error}`);
  recognition.onend = () => {
    if (running) {
      try { recognition.start(); } catch {}
    } else {
      setStatus("Stopped.");
    }
  };

  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      const text = r[0].transcript;
      if (r.isFinal) finalBuffer += " " + text;
    }
    if (finalBuffer.trim()) {
      addTranscript(finalBuffer.trim());
      finalBuffer = "";
    }
  };

  recognition.start();
}

/** ----------------- Coach loop ----------------- **/
async function startCoachLoop() {
  tipTimer = setInterval(async () => {
    const recent = getRecentText(900);
    if (!recent || recent.trim().length < 20) return;

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

  if (recognition) {
    try { recognition.onend = null; recognition.stop(); } catch {}
    recognition = null;
  }

  setStatus("Stopped.");
}

/** ----------------- UI buttons ----------------- **/
elHealth.addEventListener("click", () => {
  setStatus("Hosted version: Pro mode requires running the Local App at http://127.0.0.1:8000/");
});

elStart.addEventListener("click", async () => {
  elStart.disabled = true;
  elStop.disabled = false;
  running = true;

  await startCoachLoop();

  if (elMode.value === "local") {
    setStatus("Pro mode can’t run from GitHub Pages. Download/run the Local App at http://127.0.0.1:8000/");
    // keep running tips using rules, but no mic local whisper here
    return;
  }

  if (!supportsBrowserSpeech()) {
    setStatus("Browser Speech not supported. Use Chrome/Edge or download Pro Mode.");
    return;
  }

  startBrowserSpeech();
});

elStop.addEventListener("click", stopAll);

elClear.addEventListener("click", () => {
  transcriptText = "";
  elTranscript.textContent = "";
  setTips([]);
  setScorecard({});
});

elCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(transcriptText || "");
    setStatus("Transcript copied.");
  } catch {
    setStatus("Copy failed (browser blocked clipboard).");
  }
});

// initial state
setTips(["Click “Start Listening” to begin."]);
setScorecard({});