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
      <h1>Mock SQL Interview</h1>
      <p class="home-sub">45 minutes, spoken. The AI follows up on gaps, probes deeper on strong answers, and moves on when a topic's covered.</p>

      ${!speechRecognitionSupported ? `<div class="upsell-box">Voice input (speech-to-text) isn't supported in this browser -- Chrome or Edge recommended. You can still type your answers below.</div>` : ""}

      <div class="setup-card">
        <div class="setup-row">
          <label class="radio-label"><input type="radio" name="interviewMode" value="generic" checked /> Generic interview (covers core SQL fundamentals)</label>
          <label class="radio-label"><input type="radio" name="interviewMode" value="personalized" /> Personalized (based on my resume)</label>
        </div>

        <div id="resumeUploadRow" class="setup-row" style="display:none;">
          <input type="file" id="resumeFile" accept=".pdf,.docx" />
          <div id="resumeStatus" class="resume-status"></div>
        </div>

        <div class="setup-row">
          <label class="radio-label"><input type="checkbox" id="skipIntroCheck" /> Skip the "tell me about yourself" intro, go straight to technical questions</label>
        </div>

        <div id="setupError" class="result-banner fail" style="display:none;"></div>

        <button class="submit-btn" id="startInterviewBtn">Start Interview</button>
      </div>
    </div>
  `;

  let resumeText = null;

  document.querySelectorAll('input[name="interviewMode"]').forEach(radio => {
    radio.onchange = () => {
      document.getElementById("resumeUploadRow").style.display =
        document.querySelector('input[name="interviewMode"]:checked').value === "personalized" ? "flex" : "none";
    };
  });

  document.getElementById("resumeFile").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const statusEl = document.getElementById("resumeStatus");
    statusEl.textContent = "Parsing resume…";
    try {
      const res = await uploadResume(file);
      resumeText = res.resume_text;
      statusEl.textContent = `✅ Parsed ${file.name} (${resumeText.length} chars)`;
    } catch (err) {
      statusEl.textContent = `⚠️ ${err.message}`;
      resumeText = null;
    }
  };

  document.getElementById("startInterviewBtn").onclick = async () => {
    const mode = document.querySelector('input[name="interviewMode"]:checked').value;
    const skipIntro = document.getElementById("skipIntroCheck").checked;
    const errorEl = document.getElementById("setupError");
    errorEl.style.display = "none";

    if (mode === "personalized" && !resumeText) {
      errorEl.textContent = "Upload a resume (PDF or DOCX) first, or switch to Generic.";
      errorEl.style.display = "flex";
      return;
    }

    try {
      const res = await api("/api/interview/start", {
        method: "POST",
        body: JSON.stringify({ mode, resume_text: resumeText, skip_intro: skipIntro }),
      });
      beginLiveInterview(res, mode, resumeText);
    } catch (err) {
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
    timerHandle: null,
    transcript: [{ role: "assistant", content: startRes.question, topic: startRes.topic }],
  };
  renderLiveInterview();
  speak(startRes.question);
  startTimer();
}

function renderLiveInterview() {
  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `
    <div class="interview-live">
      <div class="interview-topbar">
        <div class="interview-timer" id="interviewTimer">${formatTime(interviewState.remainingSeconds)}</div>
        <button class="run-btn" id="endInterviewBtn">End Interview</button>
      </div>
      <div class="interview-transcript" id="interviewTranscript"></div>
      <div class="interview-controls">
        ${speechRecognitionSupported ? `
          <button class="mic-btn" id="micBtn">🎤 Hold to Answer</button>
          <div class="interim-text" id="interimText"></div>
        ` : `
          <textarea id="typedAnswer" placeholder="Type your answer…" rows="3"></textarea>
          <button class="submit-btn" id="submitTypedBtn">Submit Answer</button>
        `}
      </div>
    </div>
  `;
  renderTranscript();

  document.getElementById("endInterviewBtn").onclick = () => endInterview();

  if (speechRecognitionSupported) {
    const micBtn = document.getElementById("micBtn");
    micBtn.onmousedown = startListening;
    micBtn.onmouseup = stopListening;
    micBtn.ontouchstart = (e) => { e.preventDefault(); startListening(); };
    micBtn.ontouchend = (e) => { e.preventDefault(); stopListening(); };
  } else {
    document.getElementById("submitTypedBtn").onclick = () => {
      const text = document.getElementById("typedAnswer").value.trim();
      if (text) submitAnswer(text);
    };
  }
}

function renderTranscript() {
  const el = document.getElementById("interviewTranscript");
  if (!el) return;
  el.innerHTML = interviewState.transcript.map(t => `
    <div class="followup-turn ${t.role === "assistant" ? "assistant" : "user"}">
      <div class="who">${t.role === "assistant" ? "Interviewer" : "You"}${t.topic && t.topic !== "intro" ? ` · ${escapeHtml(t.topic)}` : ""}</div>
      ${escapeHtml(t.content)}
    </div>
  `).join("");
  el.scrollTop = el.scrollHeight;
}

function startTimer() {
  if (interviewState.timerHandle) clearInterval(interviewState.timerHandle);
  interviewState.timerHandle = setInterval(() => {
    if (!interviewState) return;
    interviewState.remainingSeconds -= 1;
    const timerEl = document.getElementById("interviewTimer");
    if (timerEl) timerEl.textContent = formatTime(interviewState.remainingSeconds);
    if (interviewState.remainingSeconds <= 0) {
      clearInterval(interviewState.timerHandle);
      endInterview();
    }
  }, 1000);
}

function speak(text) {
  return new Promise((resolve) => {
    if (!speechSynthesisSupported) { resolve(); return; }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.onend = resolve;
    utterance.onerror = resolve;
    window.speechSynthesis.speak(utterance);
  });
}

function startListening() {
  if (!speechRecognitionSupported || isListening) return;
  recognition = new SpeechRecognitionCtor();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-IN";

  let finalTranscript = "";
  const interimEl = document.getElementById("interimText");

  recognition.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const chunk = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalTranscript += chunk + " ";
      else interim += chunk;
    }
    if (interimEl) interimEl.textContent = finalTranscript + interim;
  };

  recognition.onend = () => {
    isListening = false;
    const micBtn = document.getElementById("micBtn");
    if (micBtn) micBtn.classList.remove("listening");
    const text = finalTranscript.trim();
    if (interimEl) interimEl.textContent = "";
    if (text) submitAnswer(text);
  };

  recognition.onerror = () => {
    isListening = false;
    const micBtn = document.getElementById("micBtn");
    if (micBtn) micBtn.classList.remove("listening");
  };

  isListening = true;
  const micBtn = document.getElementById("micBtn");
  if (micBtn) micBtn.classList.add("listening");
  recognition.start();
}

function stopListening() {
  if (recognition && isListening) recognition.stop();
}

async function submitAnswer(answerText) {
  interviewState.transcript.push({ role: "user", content: answerText, topic: null });
  renderTranscript();

  const micBtn = document.getElementById("micBtn");
  if (micBtn) micBtn.disabled = true;

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
    renderTranscript();
    speak(res.question);
  } catch (err) {
    interviewState.transcript.push({ role: "assistant", content: `⚠️ ${err.message}`, topic: null });
    renderTranscript();
  } finally {
    if (micBtn) micBtn.disabled = false;
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
    renderFeedback(res.feedback, res.conversation);
  } catch (err) {
    screen.innerHTML = `<div class="feedback-report">
      <div class="result-banner fail">Couldn't generate feedback: ${escapeHtml(err.message)}</div>
      <button class="submit-btn" id="backHomeBtn2">Back to home</button>
    </div>`;
    document.getElementById("backHomeBtn2").onclick = showHome;
  }
  interviewState = null;
}

function renderFeedback(report, transcript) {
  const screen = document.getElementById("interviewScreen");
  screen.innerHTML = `
    <div class="feedback-report">
      <h1>Interview Feedback</h1>
      <div class="feedback-level pill ${report.rough_level === "advanced" ? "hard" : report.rough_level === "beginner" ? "easy" : "medium"}">${escapeHtml(report.rough_level || "")}</div>
      <p class="feedback-summary">${escapeHtml(report.overall_summary || "")}</p>

      <div class="feedback-cols">
        <div class="feedback-col">
          <h3>Strengths</h3>
          <ul>${(report.strengths || []).map(s => `<li>${escapeHtml(s)}</li>`).join("") || "<li>—</li>"}</ul>
        </div>
        <div class="feedback-col">
          <h3>Weaknesses</h3>
          <ul>${(report.weaknesses || []).map(s => `<li>${escapeHtml(s)}</li>`).join("") || "<li>—</li>"}</ul>
        </div>
      </div>

      <h3>Topics to Study</h3>
      <div class="topic-pills">${(report.topics_to_study || []).map(t => `<span class="pill tag-pill">${escapeHtml(t)}</span>`).join("") || "—"}</div>

      <h3>Full Transcript</h3>
      <div class="interview-transcript static">
        ${transcript.map(t => `
          <div class="followup-turn ${t.role === "assistant" ? "assistant" : "user"}">
            <div class="who">${t.role === "assistant" ? "Interviewer" : "You"}${t.topic && t.topic !== "intro" ? ` · ${escapeHtml(t.topic)}` : ""}</div>
            ${escapeHtml(t.content)}
          </div>
        `).join("")}
      </div>

      <button class="submit-btn" id="backHomeBtn">Back to home</button>
    </div>
  `;
  document.getElementById("backHomeBtn").onclick = showHome;
}

window.renderInterviewSetup = renderInterviewSetup;
window.stopInterviewAudio = () => {
  window.speechSynthesis?.cancel();
  if (recognition && isListening) recognition.stop();
  if (interviewState && interviewState.timerHandle) clearInterval(interviewState.timerHandle);
};
