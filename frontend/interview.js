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

// Both of these bypass api()'s wrapper because they send FormData rather
// than JSON, but still need the same signed-in-identity resolution -- the
// backend prefers a verified Clerk Authorization token over the anonymous
// X-User-Id header (auth.resolve_user_id), so omitting it here silently
// misattributes the request to the browser-local anonymous id instead of
// the signed-in account (e.g. wrongly hitting that anon id's own trial gate
// even for an admin's real account).
async function authHeaders() {
  const headers = { "X-User-Id": USER_ID };
  if (typeof isSignedIn === "function" && isSignedIn()) {
    const token = await getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

async function uploadResume(file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE}/api/interview/parse-resume`, {
    method: "POST",
    headers: await authHeaders(),
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
    headers: await authHeaders(),
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
  { value: "Power Automate Developer", icon: "⚡", desc: "Flows, approvals, Teams/SharePoint/Forms integrations, Dataverse." },
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
  // Transcript starts empty -- each line is appended by speak()'s onStart,
  // right as its audio begins, so the text a candidate sees always matches
  // what they're currently hearing instead of jumping ahead of the voice.
  interviewState = {
    sessionId: startRes.session_id,
    targetRole,
    resumeText,
    remainingSeconds: startRes.remaining_seconds,
    durationSeconds: startRes.duration_seconds || 45 * 60,
    timerHandle: null,
    transcript: [],
    tableContext: startRes.table_context || null,
  };
  setActiveSessionId(startRes.session_id);
  renderLiveInterview();
  setAnswerControlsEnabled(false);
  speakingRevealIndex = null; // discard any leftover reveal state from a previous session

  const appendAssistantLine = (content, topic) => {
    interviewState.transcript.push({ role: "assistant", content, topic });
    startTextReveal(interviewState.transcript.length - 1, content, currentAudio);
  };
  // startRes.question is null when the opening monologue ended by asking
  // the history-preference question instead (see api_interview_start's
  // awaiting_history_pref branch) -- there's no second line to speak yet;
  // the candidate's spoken/typed reply goes through the normal answer flow
  // and the backend responds with the real first question from there.
  const speakChain = startRes.opening_monologue
    ? speak(startRes.opening_monologue, () => appendAssistantLine(startRes.opening_monologue, null))
        .then(() => startRes.question && speak(startRes.question, () => appendAssistantLine(startRes.question, startRes.topic)))
    : speak(startRes.question, () => appendAssistantLine(startRes.question, startRes.topic));
  speakChain.finally(() => setAnswerControlsEnabled(true));
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

// While an assistant line is mid-speech, its bubble shows only the prefix
// revealed so far (see startTextReveal) instead of the full text -- this is
// what makes the caption progress in step with the voice rather than
// appearing all at once.
let speakingRevealIndex = null;
let speakingRevealChars = 0;

function renderChatBubble(t, idx) {
  const isAssistant = t.role === "assistant";
  const isRevealing = isAssistant && idx === speakingRevealIndex;
  const content = isRevealing ? t.content.slice(0, speakingRevealChars) : t.content;
  return `
    <div class="chat-turn ${isAssistant ? "assistant" : "user"}" data-idx="${idx}">
      <div class="chat-avatar ${isAssistant ? "assistant" : "user"}">${isAssistant ? "◆" : "●"}</div>
      <div class="chat-bubble-col">
        <div class="chat-who">${isAssistant ? "Interviewer" : "You"}${t.topic && t.topic !== "intro" ? `<span class="chat-topic">${escapeHtml(t.topic)}</span>` : ""}</div>
        <div class="chat-bubble${isRevealing ? " revealing" : ""}">${escapeHtml(content)}</div>
      </div>
    </div>
  `;
}

// During a reveal, only the currently-revealing bubble's text actually
// changes tick to tick -- but renderTranscript() used to replace the
// WHOLE transcript's innerHTML on every ~120ms tick, tearing down and
// recreating every bubble's DOM node, including ones that weren't
// changing at all. Since .chat-turn has an entrance animation
// (studio-rise-in), that meant every bubble on screen replayed its
// fade/slide-in on every single tick for as long as any line was being
// revealed -- the flicker/jitter this was built to fix. Now the ticking
// interval only touches the one bubble's text node directly; a full
// renderTranscript() still runs once when a new bubble is first added
// and once when the reveal finishes, so entrance animations play
// exactly once per bubble, same as any other message.
function updateRevealingBubbleText() {
  const el = document.getElementById("interviewTranscript");
  if (!el || speakingRevealIndex === null || !interviewState) return;
  const bubble = el.querySelector(`[data-idx="${speakingRevealIndex}"] .chat-bubble`);
  const turn = interviewState.transcript[speakingRevealIndex];
  if (!bubble || !turn) { renderTranscript(); return; } // shape changed unexpectedly -- fall back to a full rebuild
  bubble.textContent = turn.content.slice(0, speakingRevealChars);
  el.scrollTop = el.scrollHeight;
}

// Real per-word timestamps aren't available from the TTS API, so this
// approximates them: reveal text proportionally to the audio element's own
// playback position (currentTime/duration), re-checked every frame. Close
// enough to read as "captions following the voice" rather than a fixed
// typing-speed guess that would drift out of sync on longer or shorter
// lines. audioEl is null for the browser-speechSynthesis fallback path, in
// which case it just reveals immediately (matches that path's pre-existing
// behavior -- true word boundaries there would need the separate
// `boundary` event API, not worth it for a rarely-hit fallback).
// Rough estimate of spoken characters/second at a natural pace (~150 wpm,
// ~5.7 chars/word including the trailing space) -- used ONLY as a fallback
// when the real audio duration isn't available (see below), never in place
// of it when it is.
const ESTIMATED_CHARS_PER_SECOND = 15;

function startTextReveal(index, fullText, audioEl) {
  speakingRevealIndex = index;
  speakingRevealChars = audioEl ? 0 : fullText.length;
  renderTranscript();
  if (!audioEl) { speakingRevealIndex = null; return; }

  // Termination uses the audio element's real "ended"/"pause" *events*,
  // not its .paused *property* -- a freshly-created <audio> reports
  // paused === true by default before playback has even begun (there's an
  // async gap between calling .play() and audio actually starting), so
  // polling the property caused the very first animation frame to see
  // "paused" and instantly reveal the whole line before any sound played.
  // The events, by contrast, only fire on a genuine pause/end transition.
  let stopped = false;
  const finish = () => {
    if (stopped || speakingRevealIndex !== index) return;
    stopped = true;
    clearInterval(intervalId);
    speakingRevealChars = fullText.length;
    speakingRevealIndex = null;
    renderTranscript();
  };
  audioEl.addEventListener("ended", finish);
  audioEl.addEventListener("pause", finish); // covers stopSpeaking() cutting playback short mid-reveal

  // Driven by setInterval, not requestAnimationFrame -- rAF is throttled to
  // near-zero by the browser the moment the tab isn't visible/focused
  // (switching tabs or apps mid-interview, a phone locking/backgrounding
  // the browser), which silently froze the reveal at 0% for the rest of
  // the clip, only jumping to the full line on the "ended" event -- i.e.
  // exactly "text doesn't show until speech is done". A ~120ms interval
  // still keeps firing (browsers only throttle it to roughly once/second
  // in the background, never fully stop it), and is more than smooth
  // enough for a caption-following-speech effect either way.
  const intervalId = setInterval(() => {
    if (stopped || speakingRevealIndex !== index || !interviewState) { clearInterval(intervalId); return; }
    // audioEl.duration is frequently Infinity/NaN for a blob-sourced MP3
    // until playback is well underway (a well-known browser quirk with
    // MediaSource/blob audio and VBR-encoded MP3s, which often lack a
    // reliable duration header). Falling back to a rough characters-per-
    // second estimate keeps currentTime (which browsers DO report
    // correctly throughout playback, real duration or not) driving a
    // smoothly advancing reveal instead of stalling at 0% until duration
    // resolves. Capped below 100% either way -- only the real "ended"/
    // "pause" event is allowed to call it complete, so an estimation
    // error can never finish the text before the audio actually does.
    const realDuration = audioEl.duration;
    const duration = isFinite(realDuration) && realDuration > 0
      ? realDuration
      : fullText.length / ESTIMATED_CHARS_PER_SECOND;
    const progress = duration > 0 ? Math.min(0.98, audioEl.currentTime / duration) : 0;
    speakingRevealChars = Math.floor(fullText.length * progress);
    updateRevealingBubbleText();
  }, 120);
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

// `onStart`, if given, fires right as playback actually begins -- not when
// speak() is called or when the text is available. The TTS call is a
// network round-trip, so rendering the transcript bubble eagerly (as soon
// as the reply text arrives) made the interviewer's words show up on screen
// well before they were heard, which read as unnatural/out of sync. Passing
// the transcript-append as onStart keeps text and voice appearing together.
function speak(text, onStart) {
  // Always stop whatever's currently playing first -- without this, two
  // speak() calls close together (e.g. a candidate answering fast enough
  // to trigger the next question before this one finishes) leave both
  // Audio objects playing at once, which sounds like garbled, overlapping
  // speech rather than a clean cut from one line to the next.
  stopSpeaking();
  let started = false;
  const fireOnStart = () => {
    if (started) return; // speakViaApi may invoke this then still fail/fall back -- never fire twice
    started = true;
    if (onStart) onStart();
  };
  return speakViaApi(text, fireOnStart).catch(() => speakViaBrowser(text, fireOnStart));
}

async function speakViaApi(text, onStart) {
  const headers = await authHeaders();
  headers["Content-Type"] = "application/json";
  return fetch(`${API_BASE}/api/interview/tts`, {
    method: "POST",
    headers,
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
      if (onStart) onStart();
      audio.play().catch((e) => { cleanup(); reject(e); });
    }));
}

// Disabled while the interviewer's line is being read aloud, so the
// candidate can't submit an answer to a question they haven't actually
// heard yet (previously the mic/submit/textarea only locked during the
// network call, then unlocked the instant the reply text arrived --
// well before the TTS audio for it had even started playing).
function setAnswerControlsEnabled(enabled) {
  const micBtn = document.getElementById("micBtn");
  const submitTypedBtn = document.getElementById("submitTypedBtn");
  const typedAnswer = document.getElementById("typedAnswer");
  if (micBtn) micBtn.disabled = !enabled;
  if (submitTypedBtn) submitTypedBtn.disabled = !enabled;
  if (typedAnswer) typedAnswer.disabled = !enabled;
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

function speakViaBrowser(text, onStart) {
  return new Promise((resolve) => {
    if (!speechSynthesisSupported) { if (onStart) onStart(); resolve(); return; }

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

    if (onStart) onStart();
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
  setAnswerControlsEnabled(false);

  try {
    const res = await api("/api/interview/answer", {
      method: "POST",
      body: JSON.stringify({ session_id: interviewState.sessionId, answer_text: answerText }),
    });

    if (res.time_up) {
      endInterview();
      return;
    }

    interviewState.remainingSeconds = res.remaining_seconds;
    // Keep controls disabled until the question has actually finished
    // being read aloud, not just rendered -- otherwise the candidate can
    // answer a question they haven't heard yet, and a fast-enough answer
    // can trigger the *next* question's speak() before this one's audio
    // has stopped, playing both at once. The transcript bubble (and any
    // table schema for this question) only appears once speech actually
    // starts, so what's on screen never gets ahead of what's been said.
    await speak(res.question, () => {
      interviewState.transcript.push({ role: "assistant", content: res.question, topic: res.topic });
      if (res.table_context) {
        interviewState.tableContext = res.table_context;
        renderTableContext();
      }
      startTextReveal(interviewState.transcript.length - 1, res.question, currentAudio);
    });
  } catch (err) {
    if (err.body && err.body.connection_issue) {
      renderConnectionIssuePrompt(() => sendAnswer(answerText));
    } else {
      interviewState.transcript.push({ role: "assistant", content: `⚠️ ${err.message}`, topic: null });
      renderTranscript();
    }
  } finally {
    setAnswerControlsEnabled(true);
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

let topicTrackMapPromise = null;
// Maps a topic name -> which practice-problem track it lives in ("sql" or
// "case"; python topics never appear in the interview's blended taxonomy,
// see role_topics.py), so a feedback pill/next-practice-plan entry knows
// which track to switch to. Lazy + cached -- only the feedback screen
// needs this, and it never changes within a session.
function loadTopicTrackMap() {
  if (!topicTrackMapPromise) {
    topicTrackMapPromise = api("/api/topics").then(r => {
      const map = new Map();
      (r.gradeable || []).forEach(t => map.set(t, "sql"));
      (r.case_da || []).forEach(t => map.set(t, "case"));
      (r.case_de || []).forEach(t => map.set(t, "case"));
      return map;
    }).catch(() => new Map());
  }
  return topicTrackMapPromise;
}

async function renderFeedback(report) {
  const screen = document.getElementById("interviewScreen");
  const topicTrack = await loadTopicTrackMap();

  const scoreHtml = typeof report.score === "number"
    ? `<div class="feedback-score">${report.score}<span>/100</span></div>`
    : "";
  // Falling back to "Intermediate" when rough_level is genuinely null (an
  // interview ended before anything was answered -- see interview_feedback's
  // answered_topics override) would show a real-sounding skill-level badge
  // for an assessment that explicitly has none. Only render the badge when
  // there's a real level to show.
  const levelHtml = report.rough_level && LEVEL_META[report.rough_level]
    ? `<div class="feedback-level-badge"><span class="feedback-level-icon">${LEVEL_META[report.rough_level].icon}</span>${escapeHtml(LEVEL_META[report.rough_level].label)}</div>`
    : "";

  const pillsHtml = (report.topics_to_study || []).map(t => {
    if (topicTrack.has(t)) {
      return `<button class="topic-pill topic-pill-link" data-topic="${escapeHtml(t)}" data-track="${topicTrack.get(t)}">${escapeHtml(t)} →</button>`;
    }
    return `<span class="topic-pill topic-pill-inert" title="No practice problems for this topic yet -- ask about it via Ask Phoenix from any practice problem.">${escapeHtml(t)}</span>`;
  }).join("") || "—";

  const trendHtml = report.trend_note
    ? `<p class="feedback-trend-note">📈 ${escapeHtml(report.trend_note)}</p>`
    : "";

  const topicScoresHtml = (report.topic_scores || []).length
    ? `
      <h3 class="feedback-section-title">Topic-wise Scores</h3>
      <div class="feedback-topic-scores">
        ${report.topic_scores.map(ts => `
          <div class="topic-score-row">
            <span class="topic-score-name">${escapeHtml(ts.topic)}</span>
            <div class="topic-score-bar-track"><div class="topic-score-bar-fill" style="width:${Math.max(0, Math.min(100, ts.score))}%"></div></div>
            <span class="topic-score-value">${ts.score}</span>
            ${ts.note ? `<span class="topic-score-note">${escapeHtml(ts.note)}</span>` : ""}
          </div>
        `).join("")}
      </div>
    `
    : "";

  const questionNotesHtml = (report.question_notes || []).length
    ? `
      <h3 class="feedback-section-title">Question-by-Question Notes</h3>
      <div class="feedback-question-notes">
        ${report.question_notes.map(q => `
          <div class="question-note-card">
            <div class="question-note-q">${escapeHtml(q.question || "")}</div>
            ${q.topic ? `<span class="chat-topic">${escapeHtml(q.topic)}</span>` : ""}
            ${q.candidate_answer_summary ? `<p class="question-note-answer"><strong>You said:</strong> ${escapeHtml(q.candidate_answer_summary)}</p>` : ""}
            ${q.assessment ? `<p class="question-note-assessment">${escapeHtml(q.assessment)}</p>` : ""}
            ${q.better_sample_answer ? `<p class="question-note-sample"><strong>A strong answer:</strong> ${escapeHtml(q.better_sample_answer)}</p>` : ""}
          </div>
        `).join("")}
      </div>
    `
    : "";

  const practicePlanHtml = (report.next_practice_plan || []).length
    ? `
      <h3 class="feedback-section-title">Next Practice Plan</h3>
      <div class="topic-pills">
        ${report.next_practice_plan.map(p => `
          <button class="topic-pill topic-pill-link" data-topic="${escapeHtml(p.topic)}" data-track="${escapeHtml(p.track || "sql")}" title="${escapeHtml(p.reason || "")}">${escapeHtml(p.topic)} →</button>
        `).join("")}
      </div>
    `
    : "";

  screen.innerHTML = `
    <div class="feedback-report">
      <div class="interview-eyebrow">Interview Complete</div>
      <div class="feedback-header">
        <h1>Your Feedback</h1>
        <div class="feedback-header-badges">
          ${scoreHtml}
          ${levelHtml}
        </div>
      </div>
      <p class="feedback-summary">${escapeHtml(report.overall_summary || "")}</p>
      ${trendHtml}

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

      ${topicScoresHtml}

      <h3 class="feedback-section-title">Topics to Study</h3>
      <div class="topic-pills">${pillsHtml}</div>

      ${practicePlanHtml}
      ${questionNotesHtml}

      <button class="submit-btn" id="backHomeBtn">Back to home</button>
    </div>
  `;
  document.getElementById("backHomeBtn").onclick = showHome;
  document.querySelectorAll(".topic-pill-link").forEach(btn => {
    btn.onclick = () => window.filterProblemsByTopic(btn.dataset.topic, btn.dataset.track);
  });
}

window.renderInterviewSetup = renderInterviewEntry;
window.stopInterviewAudio = () => {
  stopSpeaking();
  stopListening({ skipSubmit: true });
  if (interviewState && interviewState.timerHandle) clearInterval(interviewState.timerHandle);
};
