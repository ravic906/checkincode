/*
 * Mock voice interview. Text-to-speech goes through POST /api/interview/tts
 * (see backend/tts.py) for a natural interviewer voice, falling back to the
 * browser's own flat-sounding Web Speech API only if that call fails.
 * Speech-to-text records the candidate's full answer with the browser's
 * MediaRecorder API and sends it to POST /api/interview/stt (see
 * backend/stt.py) for transcription, rather than using the browser's own
 * (accent-fragile, inconsistent-across-browsers) SpeechRecognition. Either
 * way, the backend interview orchestration only ever sees plain text in and
 * out -- swapping either provider later is a backend env-var change, not a
 * frontend rewrite.
 */

let interviewState = null; // { sessionId, resumeText, mode, remainingSeconds, timerHandle, transcript: [{role, content, topic}] }
let isListening = false;

// STT now records the full answer and sends it to POST /api/interview/stt
// (Fireworks-hosted Whisper -- see backend/stt.py) instead of the browser's
// own Web Speech API, so support is gated on mic-recording APIs rather than
// SpeechRecognition.
const micRecordingSupported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
const speechSynthesisSupported = "speechSynthesis" in window;

function formatTime(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// Firefox fills range inputs natively via ::-moz-range-progress, but
// Chrome/Safari/Edge don't -- they just render whatever `background` is
// set on the element, so the "filled" portion has to be computed and
// painted as a hard-stop gradient by hand.
function updateDurationSliderFill(slider) {
  const min = Number(slider.min), max = Number(slider.max), val = Number(slider.value);
  const pct = ((val - min) / (max - min)) * 100;
  slider.style.background = `linear-gradient(90deg, var(--gold) 0%, var(--gold-2) ${pct}%, var(--studio-border) ${pct}%, var(--studio-border) 100%)`;
}

// The active session_id lives in localStorage so a page reload, browser
// crash, or tab close-and-reopen can reconnect to the same interview --
// the actual state lives in Postgres server-side, this is just the pointer.
const ACTIVE_SESSION_KEY = "sqlpractice_active_interview_session";
function setActiveSessionId(id) { localStorage.setItem(ACTIVE_SESSION_KEY, id); }
function getActiveSessionId() { return localStorage.getItem(ACTIVE_SESSION_KEY); }
function clearActiveSessionId() { localStorage.removeItem(ACTIVE_SESSION_KEY); }

async function uploadResume(file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE}/api/interview/parse-resume`, {
    method: "POST",
    headers: { "X-User-Id": USER_ID },
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Upload failed (${res.status})`);
  }
  return res.json();
}

async function transcribeAudio(blob) {
  const formData = new FormData();
  formData.append("file", blob, "answer.webm");
  if (interviewState) formData.append("session_id", interviewState.sessionId);
  const res = await fetch(`${API_BASE}/api/interview/stt`, {
    method: "POST",
    headers: { "X-User-Id": USER_ID },
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.detail || `Transcription failed (${res.status})`);
    err.body = body; // lets callers check body.connection_issue, same as api()'s error shape
    throw err;
  }
  return res.json();
}

async function renderInterviewEntry() {
  const existingId = getActiveSessionId();
  if (!existingId) { renderInterviewSetupScreen(); return; }

  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `<div class="loading-dots">Checking for an interview in progress…</div>`;

  try {
    const res = await api(`/api/interview/session/${existingId}`);
    if (res.ended || res.time_up) {
      clearActiveSessionId();
      renderInterviewSetupScreen();
      return;
    }
    renderResumePrompt(res);
  } catch (err) {
    // Session not found (expired, wrong user, etc.) -- nothing to resume.
    clearActiveSessionId();
    renderInterviewSetupScreen();
  }
}

function renderResumePrompt(sessionState) {
  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `
    <div class="interview-setup">
      <div class="interview-eyebrow">Pro · Voice Interview</div>
      <h1>Resume your interview?</h1>
      <p class="home-sub">You have an interview in progress with ${formatTime(sessionState.remaining_seconds)} left on the clock. Pick up where you left off, or discard it and start fresh.</p>
      <div class="setup-card">
        <button class="start-interview-btn" id="resumeBtn"><span>Resume Interview</span></button>
        <button class="end-interview-btn" id="discardBtn" style="align-self:center;">Discard and start over</button>
      </div>
    </div>
  `;
  document.getElementById("resumeBtn").onclick = () => resumeInterview(sessionState);
  document.getElementById("discardBtn").onclick = () => {
    clearActiveSessionId();
    renderInterviewSetupScreen();
  };
}

function resumeInterview(sessionState) {
  interviewState = {
    sessionId: sessionState.session_id,
    targetRole: sessionState.target_role,
    resumeText: null,
    remainingSeconds: sessionState.remaining_seconds,
    durationSeconds: sessionState.duration_seconds || 45 * 60,
    timerHandle: null,
    transcript: sessionState.conversation.length
      ? sessionState.conversation
      : [{ role: "assistant", content: sessionState.question, topic: sessionState.topic }],
    tableContext: sessionState.table_context || null,
  };
  renderLiveInterview();
  startTimer();
  // Don't re-speak automatically on resume -- the candidate may be
  // reconnecting mid-thought and an unprompted voice can be jarring;
  // the question is right there in the transcript to read.
}

const ROLE_CARDS = [
  { value: "Data Analyst", icon: "📊", desc: "SQL reporting, metrics, root-cause & experimentation." },
  { value: "BI Analyst", icon: "📈", desc: "Dashboards, data modeling, stakeholder-facing metrics." },
  { value: "Business Analyst", icon: "🧾", desc: "Root-cause analysis, forecasting, cross-functional SQL." },
  { value: "Product Analyst", icon: "🧪", desc: "A/B testing, growth & retention, product sense." },
  { value: "Data Engineer", icon: "🛠️", desc: "Pipelines, schema design, scaling & governance, full SQL." },
];

async function renderInterviewSetupScreen() {
  // Free-tier users get one 10-minute trial interview before the 402 wall
  // -- fetch usage fresh so the setup screen can adapt its copy/duration
  // control before they even try to start (rather than only reacting to
  // the 402 after the fact). Admins are fully unrestricted (see backend's
  // is_admin bypass in api_interview_start) -- the setup screen mirrors
  // that so the UI doesn't show trial/cap copy that no longer applies.
  let usage = null;
  try {
    usage = await api("/api/usage");
  } catch (e) {
    // Fall through with usage=null -- worst case the screen shows the
    // normal Pro copy and the real gate still applies server-side on submit.
  }
  const isUnrestricted = !!(usage && usage.is_admin);
  const isTrialEligible = !isUnrestricted && usage && usage.tier !== "paid" && !usage.interview_trial_used;
  const isPaidAtCap = !isUnrestricted && usage && usage.tier === "paid" && usage.interviews_this_month >= usage.max_interviews_per_month;
  const remainingThisMonth = usage && usage.tier === "paid" ? usage.max_interviews_per_month - usage.interviews_this_month : null;

  const screen = document.getElementById("interviewScreen");

  if (isPaidAtCap) {
    screen.innerHTML = `
      <div class="interview-setup">
        <div class="interview-eyebrow">Pro · Voice Interview</div>
        <h1>Mock Interview</h1>
        <div class="upsell-box">
          You've used all ${usage.max_interviews_per_month} mock interviews included this month. They reset at the start of next month.
        </div>
      </div>
    `;
    return;
  }

  const monthlyNote = usage && usage.tier === "paid" && !isUnrestricted
    ? `<p class="setup-quota-note">${remainingThisMonth} of ${usage.max_interviews_per_month} interviews left this month</p>`
    : "";

  screen.innerHTML = `
    <div class="interview-setup">
      <div class="interview-eyebrow">${isUnrestricted ? "Admin · Unrestricted" : isTrialEligible ? "Free Trial · 10 min" : "Pro · Voice Interview"}</div>
      <h1>Mock Interview</h1>
      <p class="home-sub">${isTrialEligible
        ? "Try one free 10-minute mock interview. Upgrade to Pro for the full 20-45 minute experience."
        : "20-45 minutes, spoken. Tailored to your target role and background -- the interviewer follows up on gaps, probes deeper on strong answers, and moves on when a topic's covered."}</p>
      ${monthlyNote}

      ${!micRecordingSupported ? `<div class="upsell-box">Voice input (speech-to-text) isn't supported in this browser — Chrome or Edge recommended. You can still type your answers below.</div>` : ""}

      <div class="setup-card">
        <div class="setup-section-label">Target role</div>
        <div class="role-cards" id="roleCards">
          ${ROLE_CARDS.map((r, i) => `
            <label class="role-card${i === 0 ? " selected" : ""}" data-value="${r.value}">
              <input type="radio" name="interviewRole" value="${r.value}" ${i === 0 ? "checked" : ""} />
              <div class="role-card-icon">${r.icon}</div>
              <div class="role-card-title">${r.value}</div>
              <div class="role-card-desc">${r.desc}</div>
            </label>
          `).join("")}
        </div>

        <div id="resumeRow" class="setup-row">
          <div class="setup-section-label">Résumé (optional, sharpens the questions)</div>
          <div id="resumeSavedRow" style="display:none;">
            <span class="resume-status ok">Using your saved résumé ✓</span>
            <button type="button" class="resume-manage-btn" id="resumeUpdateBtn">Update</button>
            <button type="button" class="resume-manage-btn" id="resumeDeleteBtn">Delete</button>
          </div>
          <label class="file-drop" id="fileDropLabel">
            <input type="file" id="resumeFile" accept=".pdf,.docx" />
            <span id="fileDropText">Choose a résumé (PDF or DOCX)</span>
          </label>
          <div id="resumeStatus" class="resume-status"></div>
        </div>

        <div class="setup-row">
          <div class="setup-section-label">Interviewer style</div>
          <div class="persona-chips" id="personaChips">
            <label class="persona-chip" data-value="friendly">
              <input type="radio" name="interviewPersona" value="friendly" />
              <span>Friendly</span>
            </label>
            <label class="persona-chip selected" data-value="neutral">
              <input type="radio" name="interviewPersona" value="neutral" checked />
              <span>Neutral</span>
            </label>
            <label class="persona-chip" data-value="strict">
              <input type="radio" name="interviewPersona" value="strict" />
              <span>Strict</span>
            </label>
          </div>
        </div>

        <div class="setup-row">
          <div class="duration-row-label">
            <span class="setup-section-label">Duration</span>
            <span class="duration-value" id="durationValue">${isTrialEligible ? "10 min (trial)" : "45 min"}</span>
          </div>
          <input type="range" id="durationSlider" class="duration-slider" min="20" max="45" step="5" value="45" ${isTrialEligible ? "disabled" : ""} />
        </div>

        <label class="toggle-row">
          <input type="checkbox" id="skipIntroCheck" />
          <span class="toggle-switch"></span>
          <span class="toggle-label">Skip the "tell me about yourself" intro</span>
        </label>

        <div id="setupError" class="result-banner fail" style="display:none;"></div>

        <button class="start-interview-btn" id="startInterviewBtn">
          <span>Start Interview</span>
        </button>
      </div>
    </div>
  `;

  // resumeText only tracks a FRESH upload made during this setup visit --
  // if usage.has_resume is true and nothing new gets uploaded, it stays
  // null and the backend falls back to the already-saved account resume
  // (see api_interview_start), so there's nothing to pass explicitly.
  let resumeText = null;

  document.querySelectorAll('.role-card').forEach(card => {
    card.addEventListener("click", () => {
      document.querySelectorAll('.role-card').forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
      card.querySelector('input[type="radio"]').checked = true;
    });
  });

  document.querySelectorAll('.persona-chip').forEach(chip => {
    chip.addEventListener("click", () => {
      document.querySelectorAll('.persona-chip').forEach(c => c.classList.remove("selected"));
      chip.classList.add("selected");
      chip.querySelector('input[type="radio"]').checked = true;
    });
  });

  const durationSlider = document.getElementById("durationSlider");
  updateDurationSliderFill(durationSlider);
  durationSlider.oninput = (e) => {
    document.getElementById("durationValue").textContent = `${e.target.value} min`;
    updateDurationSliderFill(e.target);
  };

  function showSavedResumeUi(saved) {
    document.getElementById("resumeSavedRow").style.display = saved ? "flex" : "none";
    document.getElementById("fileDropLabel").style.display = saved ? "none" : "flex";
  }
  showSavedResumeUi(!!(usage && usage.has_resume));

  async function handleResumeFile(file) {
    if (!file) return;
    const statusEl = document.getElementById("resumeStatus");
    const dropText = document.getElementById("fileDropText");
    const dropLabel = document.getElementById("fileDropLabel");
    dropText.textContent = file.name;
    dropLabel.classList.add("has-file");
    statusEl.textContent = "Parsing…";
    statusEl.className = "resume-status";
    try {
      const res = await uploadResume(file); // persists to the account server-side too, see api_parse_resume
      resumeText = res.resume_text;
      statusEl.textContent = `Parsed — ${resumeText.length.toLocaleString()} characters`;
      statusEl.className = "resume-status ok";
      showSavedResumeUi(true);
    } catch (err) {
      statusEl.textContent = err.message;
      statusEl.className = "resume-status error";
      resumeText = null;
    }
  }

  document.getElementById("resumeFile").onchange = (e) => handleResumeFile(e.target.files[0]);

  document.getElementById("resumeUpdateBtn").onclick = () => {
    showSavedResumeUi(false);
    document.getElementById("resumeFile").click();
  };

  document.getElementById("resumeDeleteBtn").onclick = async () => {
    try {
      await api("/api/interview/resume", { method: "DELETE" });
      resumeText = null;
      usage.has_resume = false;
      showSavedResumeUi(false);
      document.getElementById("resumeStatus").textContent = "";
      document.getElementById("fileDropText").textContent = "Choose a résumé (PDF or DOCX)";
      document.getElementById("fileDropLabel").classList.remove("has-file");
    } catch (err) {
      document.getElementById("resumeStatus").textContent = `Couldn't delete: ${err.message}`;
      document.getElementById("resumeStatus").className = "resume-status error";
    }
  };

  document.getElementById("startInterviewBtn").onclick = async () => {
    const startBtn = document.getElementById("startInterviewBtn");
    if (startBtn.disabled) return; // guard against a double-click firing two overlapping interviews
    const targetRole = document.querySelector('input[name="interviewRole"]:checked').value;
    const persona = document.querySelector('input[name="interviewPersona"]:checked').value;
    const skipIntro = document.getElementById("skipIntroCheck").checked;
    const durationMinutes = parseInt(document.getElementById("durationSlider").value, 10);
    const errorEl = document.getElementById("setupError");
    errorEl.style.display = "none";

    startBtn.disabled = true;
    try {
      const res = await api("/api/interview/start", {
        method: "POST",
        body: JSON.stringify({ target_role: targetRole, persona, resume_text: resumeText, skip_intro: skipIntro, duration_minutes: durationMinutes }),
      });
      beginLiveInterview(res, targetRole, resumeText);
    } catch (err) {
      startBtn.disabled = false;
      errorEl.textContent = err.status === 402
        ? `${err.message}`
        : `Couldn't start interview: ${err.message}`;
      errorEl.style.display = "flex";
      if (err.status === 402) {
        errorEl.innerHTML += `<br/><button id="upsellUpgradeBtn" style="margin-top:8px;">Upgrade now</button>`;
        document.getElementById("upsellUpgradeBtn").onclick = async () => {
          await doUpgrade();
          document.getElementById("startInterviewBtn").click();
        };
      }
    }
  };
}

function beginLiveInterview(startRes, targetRole, resumeText) {
  // Two separate opening turns, no candidate input between them: a
  // greeting/settle-in/plan monologue (always), then the actual first
  // question (the "introduce yourself" intro, or a live first question if
  // skip_intro was set) -- see api_interview_start's opening_monologue.
  const transcript = [{ role: "assistant", content: startRes.question, topic: startRes.topic }];
  if (startRes.opening_monologue) {
    transcript.unshift({ role: "assistant", content: startRes.opening_monologue, topic: null });
  }
  interviewState = {
    sessionId: startRes.session_id,
    targetRole,
    resumeText,
    remainingSeconds: startRes.remaining_seconds,
    durationSeconds: startRes.duration_seconds || 45 * 60,
    timerHandle: null,
    transcript,
    tableContext: startRes.table_context || null,
  };
  setActiveSessionId(startRes.session_id);
  renderLiveInterview();
  if (startRes.opening_monologue) {
    speak(startRes.opening_monologue).then(() => speak(startRes.question));
  } else {
    speak(startRes.question);
  }
  startTimer();
}

function renderTableContext() {
  const el = document.getElementById("tableContextPanel");
  if (!el) return;
  if (!interviewState.tableContext) { el.style.display = "none"; return; }
  const tc = interviewState.tableContext;
  el.style.display = "block";
  el.innerHTML = `
    <h4>${escapeHtml(tc.table_name || "Table")}</h4>
    <div class="schema-block">${escapeHtml(tc.schema || "")}</div>
    ${tc.sample_rows ? `<pre class="sample-rows-block">${escapeHtml(tc.sample_rows)}</pre>` : ""}
  `;
}

const TIMER_RING_CIRCUMFERENCE = 2 * Math.PI * 28;

function renderLiveInterview() {
  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `
    <div class="interview-live">
      <div class="interview-topbar">
        <div class="timer-ring-wrap" id="timerRingWrap">
          <svg class="timer-ring" viewBox="0 0 64 64" width="52" height="52">
            <circle class="timer-ring-track" cx="32" cy="32" r="28"></circle>
            <circle class="timer-ring-progress" id="timerRingProgress" cx="32" cy="32" r="28"
              stroke-dasharray="${TIMER_RING_CIRCUMFERENCE}" stroke-dashoffset="0"></circle>
          </svg>
          <div class="timer-ring-label" id="interviewTimer">${formatTime(interviewState.remainingSeconds)}</div>
        </div>
        <button class="end-interview-btn" id="endInterviewBtn">End Interview</button>
      </div>
      <div class="table-context-panel" id="tableContextPanel" style="display:none;"></div>
      <div class="interview-transcript" id="interviewTranscript"></div>
      <div class="interview-controls">
        ${micRecordingSupported ? `
          <button class="mic-btn" id="micBtn"><span class="mic-btn-icon">🎙️</span><span class="mic-btn-text">Tap to Speak</span></button>
          <div class="interim-text" id="interimText"></div>
        ` : ""}
        <div class="typed-answer-row">
          <textarea id="typedAnswer" placeholder="Or type your answer…" rows="4"></textarea>
          <button class="submit-btn" id="submitTypedBtn">Submit</button>
        </div>
      </div>
    </div>
  `;
  renderTranscript();
  renderTableContext();
  updateTimerRing(interviewState.remainingSeconds);

  document.getElementById("endInterviewBtn").onclick = () => endInterview();

  if (micRecordingSupported) {
    document.getElementById("micBtn").onclick = toggleListening;
  }

  document.getElementById("submitTypedBtn").onclick = () => {
    const el = document.getElementById("typedAnswer");
    const text = el.value.trim();
    if (!text) return;
    if (isListening) stopListening({ skipSubmit: true });
    el.value = "";
    submitAnswer(text);
  };
}

function renderChatBubble(t) {
  const isAssistant = t.role === "assistant";
  return `
    <div class="chat-turn ${isAssistant ? "assistant" : "user"}">
      <div class="chat-avatar ${isAssistant ? "assistant" : "user"}">${isAssistant ? "◆" : "●"}</div>
      <div class="chat-bubble-col">
        <div class="chat-who">${isAssistant ? "Interviewer" : "You"}${t.topic && t.topic !== "intro" ? `<span class="chat-topic">${escapeHtml(t.topic)}</span>` : ""}</div>
        <div class="chat-bubble">${escapeHtml(t.content)}</div>
      </div>
    </div>
  `;
}

function renderTranscript() {
  const el = document.getElementById("interviewTranscript");
  if (!el) return;
  el.innerHTML = interviewState.transcript.map(renderChatBubble).join("");
  el.scrollTop = el.scrollHeight;
}

function updateTimerRing(remainingSeconds) {
  const progress = document.getElementById("timerRingProgress");
  const label = document.getElementById("interviewTimer");
  if (label) label.textContent = formatTime(remainingSeconds);
  if (!progress) return;
  const total = (interviewState && interviewState.durationSeconds) || 45 * 60;
  const fraction = Math.max(0, Math.min(1, remainingSeconds / total));
  progress.style.strokeDashoffset = String(TIMER_RING_CIRCUMFERENCE * (1 - fraction));
  const wrap = document.getElementById("timerRingWrap");
  if (wrap) wrap.classList.toggle("timer-low", remainingSeconds <= 300);
}

function startTimer() {
  if (interviewState.timerHandle) clearInterval(interviewState.timerHandle);
  interviewState.timerHandle = setInterval(() => {
    if (!interviewState) return;
    interviewState.remainingSeconds -= 1;
    updateTimerRing(interviewState.remainingSeconds);
    if (interviewState.remainingSeconds <= 0) {
      clearInterval(interviewState.timerHandle);
      endInterview();
    }
  }, 1000);
}

// Chrome has two well-known speechSynthesis bugs that both look like
// "the question cuts off mid-sentence": (1) it garbage-collects the
// SpeechSynthesisUtterance if nothing outside the speak() call holds a
// reference to it, so we keep one at module scope; (2) it silently stops
// speaking on its own after a few seconds unless nudged, worked around by
// periodically pausing/resuming while an utterance is active.
let currentUtterance = null;
let speechKeepAliveTimer = null;

// Natural interviewer voice via POST /api/interview/tts (see backend/tts.py)
// instead of the browser's flat, robotic-sounding Web Speech API. Falls back
// to the old browser-TTS path on any failure (network blip, quota, provider
// outage) -- voice quality is a nice-to-have, the interview must still be
// usable if the new call fails.
let currentAudio = null;

function speak(text) {
  return speakViaApi(text).catch(() => speakViaBrowser(text));
}

function speakViaApi(text) {
  return fetch(`${API_BASE}/api/interview/tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User-Id": USER_ID },
    body: JSON.stringify({ text }),
  })
    .then((res) => {
      if (!res.ok) throw new Error(`TTS failed (${res.status})`);
      return res.blob();
    })
    .then((blob) => new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      currentAudio = audio;
      const cleanup = () => {
        URL.revokeObjectURL(url);
        currentAudio = null;
      };
      audio.onended = () => { cleanup(); resolve(); };
      audio.onerror = () => { cleanup(); reject(new Error("Audio playback failed")); };
      audio.play().catch((e) => { cleanup(); reject(e); });
    }));
}

function stopSpeaking() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  window.speechSynthesis?.cancel();
  clearInterval(speechKeepAliveTimer);
  currentUtterance = null;
}

function speakViaBrowser(text) {
  return new Promise((resolve) => {
    if (!speechSynthesisSupported) { resolve(); return; }

    window.speechSynthesis.cancel();
    clearInterval(speechKeepAliveTimer);

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    currentUtterance = utterance; // keep alive -- see note above

    const cleanup = () => {
      clearInterval(speechKeepAliveTimer);
      currentUtterance = null;
      resolve();
    };
    utterance.onend = cleanup;
    utterance.onerror = cleanup;

    window.speechSynthesis.speak(utterance);

    speechKeepAliveTimer = setInterval(() => {
      if (!window.speechSynthesis.speaking) { clearInterval(speechKeepAliveTimer); return; }
      window.speechSynthesis.pause();
      window.speechSynthesis.resume();
    }, 5000);
  });
}

let skipNextSubmit = false;
let mediaRecorder = null;
let recordedChunks = [];
let micStream = null;

function toggleListening() {
  if (isListening) stopListening();
  else startListening();
}

function setMicUi(state) {
  const micBtn = document.getElementById("micBtn");
  if (!micBtn) return;
  micBtn.classList.toggle("listening", state === "listening");
  micBtn.classList.toggle("transcribing", state === "transcribing");
  micBtn.disabled = state === "transcribing";
  const icon = state === "listening" ? "⏺️" : state === "transcribing" ? "⏳" : "🎙️";
  const text = state === "listening" ? "Recording… Tap to Stop"
    : state === "transcribing" ? "Transcribing…"
    : "Tap to Speak";
  micBtn.querySelector(".mic-btn-icon").textContent = icon;
  micBtn.querySelector(".mic-btn-text").textContent = text;
}

const MIC_ERROR_MESSAGES = {
  NotAllowedError: "Microphone access was blocked. Check your browser's site settings and allow the microphone, then try again.",
  PermissionDeniedError: "Microphone access was blocked. Check your browser's site settings and allow the microphone, then try again.",
  NotFoundError: "No microphone found. Check that one is connected and not in use by another app.",
  NotReadableError: "Couldn't access the microphone -- it may be in use by another app.",
  SecurityError: "Microphone access requires a secure connection (https).",
};

async function startListening() {
  if (!micRecordingSupported || isListening) return;
  const interimEl = document.getElementById("interimText");
  if (interimEl) interimEl.textContent = "";

  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    const msg = MIC_ERROR_MESSAGES[err.name] || `Couldn't access the microphone (${err.name}). Try typing your answer instead.`;
    if (interimEl) interimEl.textContent = msg;
    return;
  }

  recordedChunks = [];
  const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
  mediaRecorder = mimeType ? new MediaRecorder(micStream, { mimeType }) : new MediaRecorder(micStream);

  mediaRecorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) recordedChunks.push(event.data);
  };

  mediaRecorder.onstop = async () => {
    micStream.getTracks().forEach((track) => track.stop());
    micStream = null;
    isListening = false;

    if (skipNextSubmit) {
      skipNextSubmit = false;
      setMicUi("idle");
      return;
    }
    if (!recordedChunks.length) {
      setMicUi("idle");
      return;
    }

    setMicUi("transcribing");
    const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "audio/webm" });
    await attemptTranscribe(blob);
  };

  skipNextSubmit = false;
  isListening = true;
  setMicUi("listening");
  mediaRecorder.start();
}

async function attemptTranscribe(blob) {
  const interimEl = document.getElementById("interimText");
  try {
    const { text } = await transcribeAudio(blob);
    setMicUi("idle");
    const trimmed = (text || "").trim();
    if (trimmed) submitAnswer(trimmed);
    else if (interimEl) interimEl.textContent = "Didn't catch any speech. Try again.";
  } catch (err) {
    setMicUi("idle");
    if (err.body && err.body.connection_issue) {
      renderConnectionIssuePrompt(() => attemptTranscribe(blob));
    } else if (interimEl) {
      interimEl.textContent = err.message || "Transcription failed. Try again.";
    }
  }
}

function stopListening(opts = {}) {
  if (opts.skipSubmit) skipNextSubmit = true;
  if (mediaRecorder && isListening) mediaRecorder.stop();
}

async function submitAnswer(answerText) {
  interviewState.transcript.push({ role: "user", content: answerText, topic: null });
  renderTranscript();
  await sendAnswer(answerText);
}

// Split from submitAnswer so a connection-issue retry can resend the SAME
// answer_text without re-pushing a duplicate user bubble into the transcript.
async function sendAnswer(answerText) {
  const micBtn = document.getElementById("micBtn");
  const submitTypedBtn = document.getElementById("submitTypedBtn");
  if (micBtn) micBtn.disabled = true;
  if (submitTypedBtn) submitTypedBtn.disabled = true;

  try {
    const res = await api("/api/interview/answer", {
      method: "POST",
      body: JSON.stringify({ session_id: interviewState.sessionId, answer_text: answerText }),
    });

    if (res.time_up) {
      endInterview();
      return;
    }

    interviewState.transcript.push({ role: "assistant", content: res.question, topic: res.topic });
    interviewState.remainingSeconds = res.remaining_seconds;
    if (res.table_context) {
      interviewState.tableContext = res.table_context;
      renderTableContext();
    }
    renderTranscript();
    speak(res.question);
  } catch (err) {
    if (err.body && err.body.connection_issue) {
      renderConnectionIssuePrompt(() => sendAnswer(answerText));
    } else {
      interviewState.transcript.push({ role: "assistant", content: `⚠️ ${err.message}`, topic: null });
      renderTranscript();
    }
  } finally {
    if (micBtn) micBtn.disabled = false;
    if (submitTypedBtn) submitTypedBtn.disabled = false;
  }
}

// Shown after CONNECTION_ISSUE_THRESHOLD consecutive STT/LLM failures (see
// backend's interview.record_failure) -- offers a real choice instead of
// just erroring again on the next attempt too. "Pause" needs no new backend
// state: the existing GET /api/interview/session/{id} resume mechanic
// already lets the candidate leave and pick back up later.
function renderConnectionIssuePrompt(retryFn) {
  const container = document.getElementById("interviewTranscript");
  if (!container) return;
  const div = document.createElement("div");
  div.className = "connection-issue-banner";
  div.innerHTML = `
    <p>Having some trouble connecting. What would you like to do?</p>
    <div class="connection-issue-actions">
      <button class="connection-issue-btn" id="connIssueRetry">Try again</button>
      <button class="connection-issue-btn" id="connIssuePause">Pause (resume later)</button>
      <button class="connection-issue-btn" id="connIssueEnd">End now</button>
    </div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  document.getElementById("connIssueRetry").onclick = () => { div.remove(); retryFn(); };
  document.getElementById("connIssuePause").onclick = () => {
    div.remove();
    stopSpeaking();
    stopListening({ skipSubmit: true });
    showHome();
  };
  document.getElementById("connIssueEnd").onclick = () => { div.remove(); endInterview(); };
}

async function endInterview() {
  if (!interviewState) return;
  if (interviewState.timerHandle) clearInterval(interviewState.timerHandle);
  stopSpeaking();
  stopListening({ skipSubmit: true });

  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `<div class="loading-dots">Generating your feedback report…</div>`;

  try {
    const res = await api("/api/interview/end", {
      method: "POST",
      body: JSON.stringify({ session_id: interviewState.sessionId }),
    });
    clearActiveSessionId();
    await renderFeedback(res.feedback);
  } catch (err) {
    screen.innerHTML = `<div class="feedback-report">
      <div class="result-banner fail">Couldn't generate feedback: ${escapeHtml(err.message)}</div>
      <button class="submit-btn" id="backHomeBtn2">Back to home</button>
    </div>`;
    document.getElementById("backHomeBtn2").onclick = showHome;
  }
  interviewState = null;
}

const LEVEL_META = {
  beginner: { icon: "○", label: "Beginner" },
  intermediate: { icon: "◐", label: "Intermediate" },
  advanced: { icon: "●", label: "Advanced" },
};

let gradeableTopicsPromise = null;
function loadGradeableTopics() {
  // Lazy + cached -- only the feedback screen needs this, and it never
  // changes within a session.
  if (!gradeableTopicsPromise) {
    gradeableTopicsPromise = api("/api/topics").then(r => new Set(r.gradeable)).catch(() => new Set());
  }
  return gradeableTopicsPromise;
}

async function renderFeedback(report) {
  const screen = document.getElementById("interviewScreen");
  const level = LEVEL_META[report.rough_level] || LEVEL_META.intermediate;
  const gradeableTopics = await loadGradeableTopics();

  const scoreHtml = typeof report.score === "number"
    ? `<div class="feedback-score">${report.score}<span>/100</span></div>`
    : "";

  const pillsHtml = (report.topics_to_study || []).map(t => {
    if (gradeableTopics.has(t)) {
      return `<button class="topic-pill topic-pill-link" data-topic="${escapeHtml(t)}">${escapeHtml(t)} →</button>`;
    }
    return `<span class="topic-pill topic-pill-inert" title="No practice problems for this topic yet -- ask about it via Ask Phoenix from any practice problem.">${escapeHtml(t)}</span>`;
  }).join("") || "—";

  screen.innerHTML = `
    <div class="feedback-report">
      <div class="interview-eyebrow">Interview Complete</div>
      <div class="feedback-header">
        <h1>Your Feedback</h1>
        <div class="feedback-header-badges">
          ${scoreHtml}
          <div class="feedback-level-badge">
            <span class="feedback-level-icon">${level.icon}</span>${escapeHtml(level.label)}
          </div>
        </div>
      </div>
      <p class="feedback-summary">${escapeHtml(report.overall_summary || "")}</p>

      <div class="feedback-cols">
        <div class="feedback-col strengths">
          <h3><span class="feedback-col-icon">＋</span>Strengths</h3>
          <ul>${(report.strengths || []).map(s => `<li>${escapeHtml(s)}</li>`).join("") || "<li>—</li>"}</ul>
        </div>
        <div class="feedback-col weaknesses">
          <h3><span class="feedback-col-icon">－</span>Weaknesses</h3>
          <ul>${(report.weaknesses || []).map(s => `<li>${escapeHtml(s)}</li>`).join("") || "<li>—</li>"}</ul>
        </div>
      </div>

      <h3 class="feedback-section-title">Topics to Study</h3>
      <div class="topic-pills">${pillsHtml}</div>

      <button class="submit-btn" id="backHomeBtn">Back to home</button>
    </div>
  `;
  document.getElementById("backHomeBtn").onclick = showHome;
  document.querySelectorAll(".topic-pill-link").forEach(btn => {
    btn.onclick = () => window.filterProblemsByTopic(btn.dataset.topic);
  });
}

window.renderInterviewSetup = renderInterviewEntry;
window.stopInterviewAudio = () => {
  stopSpeaking();
  stopListening({ skipSubmit: true });
  if (interviewState && interviewState.timerHandle) clearInterval(interviewState.timerHandle);
};
