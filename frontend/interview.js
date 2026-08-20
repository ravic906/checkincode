/*
 * Mock voice interview. All speech-to-text and text-to-speech runs in the
 * browser via the Web Speech API -- no audio ever touches the backend, it
 * only ever sees transcribed text in and a spoken question text out. That
 * keeps this swappable later (e.g. to Groq Whisper for STT) without
 * touching the interview orchestration logic on the server.
 */

let interviewState = null; // { sessionId, resumeText, mode, remainingSeconds, timerHandle, transcript: [{role, content, topic}] }
let recognition = null;
let isListening = false;

const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
const speechRecognitionSupported = !!SpeechRecognitionCtor;
const speechSynthesisSupported = "speechSynthesis" in window;

function formatTime(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

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

function renderInterviewSetup() {
  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `
    <div class="interview-setup">
      <div class="interview-eyebrow">Pro · Voice Interview</div>
      <h1>Mock SQL Interview</h1>
      <p class="home-sub">20-45 minutes, spoken. The interviewer follows up on gaps, probes deeper on strong answers, and moves on when a topic's covered.</p>

      ${!speechRecognitionSupported ? `<div class="upsell-box">Voice input (speech-to-text) isn't supported in this browser — Chrome or Edge recommended. You can still type your answers below.</div>` : ""}

      <div class="setup-card">
        <div class="setup-section-label">Format</div>
        <div class="mode-cards" id="modeCards">
          <label class="mode-card selected" data-value="generic">
            <input type="radio" name="interviewMode" value="generic" checked />
            <div class="mode-card-icon">📋</div>
            <div class="mode-card-title">Generic</div>
            <div class="mode-card-desc">Core SQL fundamentals — joins, aggregation, subqueries, window functions.</div>
          </label>
          <label class="mode-card" data-value="personalized">
            <input type="radio" name="interviewMode" value="personalized" />
            <div class="mode-card-icon">📄</div>
            <div class="mode-card-title">Personalized</div>
            <div class="mode-card-desc">Grounded in your actual resume and experience.</div>
          </label>
        </div>

        <div id="resumeUploadRow" class="setup-row" style="display:none;">
          <label class="file-drop" id="fileDropLabel">
            <input type="file" id="resumeFile" accept=".pdf,.docx" />
            <span id="fileDropText">Choose a résumé (PDF or DOCX)</span>
          </label>
          <div id="resumeStatus" class="resume-status"></div>
        </div>

        <div class="setup-row">
          <div class="duration-row-label">
            <span class="setup-section-label">Duration</span>
            <span class="duration-value" id="durationValue">45 min</span>
          </div>
          <input type="range" id="durationSlider" class="duration-slider" min="20" max="45" step="5" value="45" />
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

  let resumeText = null;

  document.querySelectorAll('.mode-card').forEach(card => {
    card.addEventListener("click", () => {
      document.querySelectorAll('.mode-card').forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
      card.querySelector('input[type="radio"]').checked = true;
      document.getElementById("resumeUploadRow").style.display =
        card.dataset.value === "personalized" ? "flex" : "none";
    });
  });

  document.getElementById("durationSlider").oninput = (e) => {
    document.getElementById("durationValue").textContent = `${e.target.value} min`;
  };

  document.getElementById("resumeFile").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const statusEl = document.getElementById("resumeStatus");
    const dropText = document.getElementById("fileDropText");
    const dropLabel = document.getElementById("fileDropLabel");
    dropText.textContent = file.name;
    dropLabel.classList.add("has-file");
    statusEl.textContent = "Parsing…";
    statusEl.className = "resume-status";
    try {
      const res = await uploadResume(file);
      resumeText = res.resume_text;
      statusEl.textContent = `Parsed — ${resumeText.length.toLocaleString()} characters`;
      statusEl.className = "resume-status ok";
    } catch (err) {
      statusEl.textContent = err.message;
      statusEl.className = "resume-status error";
      resumeText = null;
    }
  };

  document.getElementById("startInterviewBtn").onclick = async () => {
    const startBtn = document.getElementById("startInterviewBtn");
    if (startBtn.disabled) return; // guard against a double-click firing two overlapping interviews
    const mode = document.querySelector('input[name="interviewMode"]:checked').value;
    const skipIntro = document.getElementById("skipIntroCheck").checked;
    const durationMinutes = parseInt(document.getElementById("durationSlider").value, 10);
    const errorEl = document.getElementById("setupError");
    errorEl.style.display = "none";

    if (mode === "personalized" && !resumeText) {
      errorEl.textContent = "Upload a resume (PDF or DOCX) first, or switch to Generic.";
      errorEl.style.display = "flex";
      return;
    }

    startBtn.disabled = true;
    try {
      const res = await api("/api/interview/start", {
        method: "POST",
        body: JSON.stringify({ mode, resume_text: resumeText, skip_intro: skipIntro, duration_minutes: durationMinutes }),
      });
      beginLiveInterview(res, mode, resumeText);
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

function beginLiveInterview(startRes, mode, resumeText) {
  interviewState = {
    sessionId: startRes.session_id,
    mode,
    resumeText,
    remainingSeconds: startRes.remaining_seconds,
    durationSeconds: startRes.duration_seconds || 45 * 60,
    timerHandle: null,
    transcript: [{ role: "assistant", content: startRes.question, topic: startRes.topic }],
    tableContext: startRes.table_context || null,
  };
  renderLiveInterview();
  speak(startRes.question);
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
        ${speechRecognitionSupported ? `
          <button class="mic-btn" id="micBtn"><span class="mic-btn-icon">🎙️</span><span class="mic-btn-text">Unmute to Speak</span></button>
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

  if (speechRecognitionSupported) {
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

function speak(text) {
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

let pendingFinalTranscript = "";
let skipNextSubmit = false;

function toggleListening() {
  if (isListening) stopListening();
  else startListening();
}

function setMicUi(state) {
  const micBtn = document.getElementById("micBtn");
  if (!micBtn) return;
  micBtn.classList.toggle("listening", state === "listening");
  micBtn.querySelector(".mic-btn-icon").textContent = state === "listening" ? "⏺️" : "🎙️";
  micBtn.querySelector(".mic-btn-text").textContent = state === "listening" ? "Listening… Tap to Mute" : "Unmute to Speak";
}

const RECOGNITION_ERROR_MESSAGES = {
  "not-allowed": "Microphone access was blocked. Check your browser's site settings and allow the microphone, then try again.",
  "no-speech": "Didn't catch any speech. Try again, and speak right after unmuting.",
  "audio-capture": "No microphone found. Check that one is connected and not in use by another app.",
  "network": "A network error interrupted speech recognition. Try again.",
  "language-not-supported": "This browser doesn't support English speech recognition. Try typing your answer instead.",
  "service-not-allowed": "Speech recognition service was blocked. Try typing your answer instead.",
};

function startListening() {
  if (!speechRecognitionSupported || isListening) return;
  recognition = new SpeechRecognitionCtor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  pendingFinalTranscript = "";
  skipNextSubmit = false;
  let lastError = null;
  const interimEl = document.getElementById("interimText");

  recognition.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const chunk = event.results[i][0].transcript;
      if (event.results[i].isFinal) pendingFinalTranscript += chunk + " ";
      else interim += chunk;
    }
    if (interimEl) interimEl.textContent = pendingFinalTranscript + interim;
  };

  recognition.onend = () => {
    isListening = false;
    setMicUi("muted");
    const text = pendingFinalTranscript.trim();
    if (lastError) {
      if (interimEl) interimEl.textContent = lastError;
    } else {
      if (interimEl) interimEl.textContent = "";
      if (text && !skipNextSubmit) submitAnswer(text);
    }
    skipNextSubmit = false;
  };

  recognition.onerror = (event) => {
    isListening = false;
    lastError = RECOGNITION_ERROR_MESSAGES[event.error] || `Speech recognition error: ${event.error}. Try typing your answer instead.`;
    setMicUi("muted");
  };

  isListening = true;
  setMicUi("listening");
  recognition.start();
}

function stopListening(opts = {}) {
  if (opts.skipSubmit) skipNextSubmit = true;
  if (recognition && isListening) recognition.stop();
}

async function submitAnswer(answerText) {
  interviewState.transcript.push({ role: "user", content: answerText, topic: null });
  renderTranscript();

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
    interviewState.transcript.push({ role: "assistant", content: `⚠️ ${err.message}`, topic: null });
    renderTranscript();
  } finally {
    if (micBtn) micBtn.disabled = false;
    if (submitTypedBtn) submitTypedBtn.disabled = false;
  }
}

async function endInterview() {
  if (!interviewState) return;
  if (interviewState.timerHandle) clearInterval(interviewState.timerHandle);
  window.speechSynthesis?.cancel();
  if (recognition && isListening) recognition.stop();

  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `<div class="loading-dots">Generating your feedback report…</div>`;

  try {
    const res = await api("/api/interview/end", {
      method: "POST",
      body: JSON.stringify({ session_id: interviewState.sessionId }),
    });
    renderFeedback(res.feedback);
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

function renderFeedback(report) {
  const screen = document.getElementById("interviewScreen");
  const level = LEVEL_META[report.rough_level] || LEVEL_META.intermediate;
  screen.innerHTML = `
    <div class="feedback-report">
      <div class="interview-eyebrow">Interview Complete</div>
      <div class="feedback-header">
        <h1>Your Feedback</h1>
        <div class="feedback-level-badge">
          <span class="feedback-level-icon">${level.icon}</span>${escapeHtml(level.label)}
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
      <div class="topic-pills">${(report.topics_to_study || []).map(t => `<span class="topic-pill">${escapeHtml(t)}</span>`).join("") || "—"}</div>

      <button class="submit-btn" id="backHomeBtn">Back to home</button>
    </div>
  `;
  document.getElementById("backHomeBtn").onclick = showHome;
}

window.renderInterviewSetup = renderInterviewSetup;
window.stopInterviewAudio = () => {
  window.speechSynthesis?.cancel();
  clearInterval(speechKeepAliveTimer);
  currentUtterance = null;
  if (recognition && isListening) recognition.stop();
  if (interviewState && interviewState.timerHandle) clearInterval(interviewState.timerHandle);
};
