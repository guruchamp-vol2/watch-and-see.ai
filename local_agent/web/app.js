// Watch & See - lightweight UI

const el = (id) => document.getElementById(id);

const healthDot = el('healthDot');
const healthText = el('healthText');
const errBox = el('errBox');
const sysInfo = el('sysInfo');

const agentUrlInput = el('agentUrl');
const btnMic = el('btnMic');
const btnStopMic = el('btnStopMic');
const btnSys = el('btnSys');
const btnStopSys = el('btnStopSys');
const btnClear = el('btnClear');
const btnCopy = el('btnCopy');

const transcriptEl = el('transcript');
const tipsEl = el('tips');
const coachSourceEl = el('coachSource');
const modeLabelEl = el('modeLabel');

const scClarity = el('scClarity');
const scEmpathy = el('scEmpathy');
const scNext = el('scNext');
const scComp = el('scComp');

const micInterval = el('micInterval');
const micIntervalVal = el('micIntervalVal');
const coachInterval = el('coachInterval');
const coachIntervalVal = el('coachIntervalVal');

const deviceSelect = el('deviceSelect');
const btnApplyDevice = el('btnApplyDevice');

// ------------------------------
// State
// ------------------------------

let micStream = null;
let mediaRecorder = null;
let micRunning = false;
let sysRunning = false;

let pollTimer = null;
let coachTimer = null;

let lastSystemTs = 0;
let lastMicText = '';

function getAgentBase() {
  const v = (agentUrlInput.value || '').trim();
  return v || `${location.protocol}//${location.hostname}:8000`;
}

function api(path) {
  return `${getAgentBase()}${path}`;
}

function setError(msg) {
  if (!msg) {
    errBox.style.display = 'none';
    errBox.textContent = '';
    return;
  }
  errBox.style.display = 'block';
  errBox.textContent = msg;
}

function setHealth(ok, text) {
  healthDot.style.background = ok ? 'var(--good)' : 'var(--bad)';
  healthDot.style.boxShadow = ok
    ? '0 0 0 4px rgba(40,199,111,.12)'
    : '0 0 0 4px rgba(234,84,85,.12)';
  healthText.textContent = text;
}

function setModeLabel() {
  if (micRunning) modeLabelEl.textContent = 'Mic';
  else if (sysRunning) modeLabelEl.textContent = 'System Audio';
  else modeLabelEl.textContent = 'Idle';
}

function appendTranscript(text) {
  if (!text) return;
  const current = transcriptEl.value || '';
  transcriptEl.value = (current ? current + ' ' : '') + text;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function renderTips(tips, scorecard, source) {
  tipsEl.innerHTML = '';
  coachSourceEl.textContent = source ? `source: ${source}` : '—';

  if (!tips || tips.length === 0) {
    tipsEl.innerHTML = '<div class="empty">No tips yet…</div>';
  } else {
    for (const t of tips) {
      const div = document.createElement('div');
      div.className = 'tip';
      div.textContent = t;
      tipsEl.appendChild(div);
    }
  }

  const get = (k) => (scorecard && typeof scorecard[k] !== 'undefined' ? scorecard[k] : '—');
  scClarity.textContent = get('clarity');
  scEmpathy.textContent = get('empathy');
  scNext.textContent = get('next_steps');
  scComp.textContent = get('compliance');
}

async function jsonFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}${t ? ` — ${t}` : ''}`);
  }
  return res.json();
}

// ------------------------------
// Health + devices
// ------------------------------

async function refreshHealth() {
  try {
    const h = await jsonFetch(api('/api/health'));
    const o = h.ollama_ok ? 'Ollama ✓' : 'Ollama ✕';
    setHealth(true, `Agent OK • Whisper: ${h.whisper_model} • ${o}`);

    const sys = h.system_audio || {};
    const dev = sys.device_name ? `${sys.device_name} (${sys.device_hostapi || '?'})` : '—';
    const forced = typeof sys.forced_index === 'number' ? `forced: ${sys.forced_index}` : '';
    sysInfo.textContent = `System audio: ${dev} ${forced}`.trim();

    if (sys.error) setError(sys.error);
  } catch (e) {
    setHealth(false, 'Agent offline');
  }
}

async function loadDevices() {
  try {
    const data = await jsonFetch(api('/api/audio_devices'));
    const devices = data.devices || [];
    deviceSelect.innerHTML = '';

    // Add "auto" option
    const optAuto = document.createElement('option');
    optAuto.value = '';
    optAuto.textContent = 'Auto (recommended)';
    deviceSelect.appendChild(optAuto);

    for (const d of devices) {
      // Only show inputs
      if (!d.max_input_channels || d.max_input_channels <= 0) continue;
      const opt = document.createElement('option');
      opt.value = String(d.index);
      opt.textContent = `${d.index} — ${d.name} • ${d.hostapi} • in:${d.max_input_channels} • sr:${Math.round(d.default_samplerate || 0)}`;
      deviceSelect.appendChild(opt);
    }
  } catch {
    // ignore
  }
}

// ------------------------------
// System audio polling
// ------------------------------

async function pollSystemLatest() {
  try {
    const data = await jsonFetch(api('/api/system_audio/latest'));
    if (data.error) {
      setError(data.error);
      return;
    }
    if (data.ts && data.ts > lastSystemTs) {
      lastSystemTs = data.ts;
      // Replace entire transcript (server keeps de-dup'd full text)
      transcriptEl.value = data.text || '';
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } catch (e) {
    setError(String(e.message || e));
  }
}

function startPollLoop() {
  stopPollLoop();
  pollTimer = setInterval(() => {
    if (sysRunning) pollSystemLatest();
    refreshHealth();
  }, 600);
}

function stopPollLoop() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

// ------------------------------
// Coach loop
// ------------------------------

async function tickCoach() {
  const t = (transcriptEl.value || '').trim();
  if (t.length < 10) return;
  try {
    const res = await jsonFetch(api('/api/coach'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: t.slice(-2000) }),
    });
    renderTips(res.tips || [], res.scorecard || {}, res.source || '');
  } catch (e) {
    setError(String(e.message || e));
  }
}

function startCoachLoop() {
  stopCoachLoop();
  const ms = Number(coachInterval.value || 1600);
  coachTimer = setInterval(tickCoach, ms);
}

function stopCoachLoop() {
  if (coachTimer) clearInterval(coachTimer);
  coachTimer = null;
}

// ------------------------------
// Mic capture
// ------------------------------

async function startMic() {
  setError('');
  if (micRunning) return;
  if (sysRunning) await stopSystemAudio();

  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';

    mediaRecorder = new MediaRecorder(micStream, { mimeType: mime });
    const chunks = [];

    mediaRecorder.ondataavailable = async (e) => {
      if (!e.data || e.data.size === 0) return;
      chunks.push(e.data);
      const blob = new Blob(chunks.splice(0, chunks.length), { type: mime });
      const fd = new FormData();
      fd.append('audio', blob, 'chunk.webm');

      try {
        const r = await fetch(api('/api/transcribe'), { method: 'POST', body: fd });
        const j = await r.json();
        const text = (j.text || '').trim();
        if (text && text !== lastMicText) {
          lastMicText = text;
          appendTranscript(text);
        }
      } catch (err) {
        setError(String(err.message || err));
      }
    };

    const intervalMs = Number(micInterval.value || 1200);
    mediaRecorder.start(intervalMs);

    micRunning = true;
    btnMic.disabled = true;
    btnStopMic.disabled = false;
    btnSys.disabled = true;
    btnStopSys.disabled = true;
    setModeLabel();
    startCoachLoop();
  } catch (e) {
    setError(`Mic error: ${String(e.message || e)}`);
  }
}

async function stopMic() {
  if (!micRunning) return;
  try {
    mediaRecorder?.stop();
  } catch {}
  try {
    micStream?.getTracks()?.forEach((t) => t.stop());
  } catch {}

  mediaRecorder = null;
  micStream = null;
  micRunning = false;
  btnMic.disabled = false;
  btnStopMic.disabled = true;
  btnSys.disabled = false;
  setModeLabel();
}

// ------------------------------
// System audio control
// ------------------------------

async function startSystemAudio() {
  setError('');
  if (sysRunning) return;
  if (micRunning) await stopMic();

  try {
    await jsonFetch(api('/api/system_audio/start'), { method: 'POST' });
    sysRunning = true;
    lastSystemTs = 0;

    btnSys.disabled = true;
    btnStopSys.disabled = false;
    btnMic.disabled = true;
    btnStopMic.disabled = true;
    setModeLabel();
    startPollLoop();
    startCoachLoop();
  } catch (e) {
    setError(`System audio error: ${String(e.message || e)}`);
  }
}

async function stopSystemAudio() {
  if (!sysRunning) return;
  try {
    await jsonFetch(api('/api/system_audio/stop'), { method: 'POST' });
  } catch {}
  sysRunning = false;
  btnSys.disabled = false;
  btnStopSys.disabled = true;
  btnMic.disabled = false;
  setModeLabel();
}

async function applyDevice() {
  const v = deviceSelect.value;
  const index = v === '' ? null : Number(v);
  try {
    await jsonFetch(api('/api/system_audio/select'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index }),
    });
    await refreshHealth();
    setError('');
  } catch (e) {
    setError(`Device select error: ${String(e.message || e)}`);
  }
}

// ------------------------------
// UI wiring
// ------------------------------

btnMic.addEventListener('click', startMic);
btnStopMic.addEventListener('click', stopMic);
btnSys.addEventListener('click', startSystemAudio);
btnStopSys.addEventListener('click', stopSystemAudio);

btnClear.addEventListener('click', () => {
  transcriptEl.value = '';
  lastSystemTs = 0;
  lastMicText = '';
  renderTips([], {}, '');
});

btnCopy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(transcriptEl.value || '');
    btnCopy.textContent = 'Copied!';
    setTimeout(() => (btnCopy.textContent = 'Copy'), 900);
  } catch {
    // fallback
    transcriptEl.select();
    document.execCommand('copy');
  }
});

micInterval.addEventListener('input', () => {
  micIntervalVal.textContent = String(micInterval.value);
  if (micRunning) {
    // restart recorder to apply
    stopMic().then(startMic);
  }
});

coachInterval.addEventListener('input', () => {
  coachIntervalVal.textContent = String(coachInterval.value);
  if (micRunning || sysRunning) startCoachLoop();
});

btnApplyDevice.addEventListener('click', applyDevice);

agentUrlInput.addEventListener('change', async () => {
  localStorage.setItem('ws_agent_url', agentUrlInput.value.trim());
  await refreshHealth();
  await loadDevices();
});

// Init
(function init() {
  agentUrlInput.value = localStorage.getItem('ws_agent_url') || 'http://127.0.0.1:8000';
  micIntervalVal.textContent = String(micInterval.value);
  coachIntervalVal.textContent = String(coachInterval.value);
  setModeLabel();
  refreshHealth();
  loadDevices();
  startPollLoop();
})();