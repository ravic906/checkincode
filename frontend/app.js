const API_BASE = window.API_BASE || "http://127.0.0.1:8000";

function getUserId() {
  let id = localStorage.getItem("sqlpractice_user_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("sqlpractice_user_id", id);
  }
  return id;
}
const USER_ID = getUserId();

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-User-Id": USER_ID,
    ...(options.headers || {}),
  };
  // When signed in, prefer the verified Clerk identity over the anonymous
  // X-User-Id -- the backend trusts this token over the header if both are
  // present (see auth.resolve_user_id in main.py).
  if (typeof isSignedIn === "function" && isSignedIn()) {
    const token = await getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

let allProblems = [];
let currentProblem = null;
let monacoEditor = null;

// Follow-up chat state for the currently-shown result. Reset on every new run/submit.
let followupState = null; // { studentQuery, expectedPreview, actualPreview, error, conversation: [{role, content}] }

function getTutorEnabled() {
  // Explain is a signed-in feature -- the toggle that controls it is
  // hidden pre-sign-in (see updateSignInGatedUI), so don't silently keep
  // requesting explanations behind the scenes for a control the user
  // can't see or turn off.
  if (typeof isSignedIn !== "function" || !isSignedIn()) return false;
  const v = localStorage.getItem("sqlpractice_tutor_enabled");
  return v === null ? true : v === "true";
}
function setTutorEnabled(enabled) {
  localStorage.setItem("sqlpractice_tutor_enabled", String(enabled));
}

let _onPracticeScreen = false;

function updateSignInGatedUI() {
  // Reset Progress and the Explain toggle only make sense (a) while on
  // the practice screen, where there's actually something to reset/
  // explain, and (b) once signed in -- an anonymous browser id isn't a
  // real account, so offering account-flavored controls before sign-in
  // is just confusing.
  const signedIn = typeof isSignedIn === "function" && isSignedIn();
  const show = _onPracticeScreen && signedIn;
  document.getElementById("resetProgressBtn").style.display = show ? "inline-block" : "none";
  document.getElementById("tutorToggleWrap").style.display = show ? "flex" : "none";
}

function showHome() {
  document.getElementById("homeScreen").style.display = "flex";
  document.getElementById("practiceLayout").style.display = "none";
  document.getElementById("interviewScreen").style.display = "none";
  _onPracticeScreen = false;
  updateSignInGatedUI();
  if (window.stopInterviewAudio) window.stopInterviewAudio();
}
function showSqlTrack() {
  document.getElementById("homeScreen").style.display = "none";
  document.getElementById("practiceLayout").style.display = "flex";
  document.getElementById("interviewScreen").style.display = "none";
  _onPracticeScreen = true;
  updateSignInGatedUI();
}
function showInterviewScreen() {
  document.getElementById("homeScreen").style.display = "none";
  document.getElementById("practiceLayout").style.display = "none";
  document.getElementById("interviewScreen").style.display = "flex";
  _onPracticeScreen = false;
  updateSignInGatedUI();
}

async function refreshTierBadge() {
  const usage = await api("/api/usage");
  const badge = document.getElementById("tierBadge");
  badge.classList.toggle("paid", usage.tier === "paid");
  if (usage.tier === "paid") {
    badge.innerHTML = `Pro — full problem library, unlimited explanations`;
  } else {
    // The daily submission/explanation counters rarely bind in practice --
    // the free-tier problem lock is the restriction that actually
    // matters, so lead with that instead of burying it behind counters
    // that look generous on their own ("0/20 submissions" reads like
    // broad access). Deliberately no exact counts here (bank size and
    // free-tier fraction aren't things we want to publish in the UI).
    badge.innerHTML = `Free — limited problem access <button id="upgradeBtn">Upgrade ₹199/mo</button>`;
  }
  const btn = document.getElementById("upgradeBtn");
  if (btn) btn.onclick = doUpgrade;
  return usage;
}

let razorpayScriptPromise = null;
function loadRazorpayScript() {
  if (razorpayScriptPromise) return razorpayScriptPromise;
  razorpayScriptPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = () => resolve();
    s.onerror = (e) => reject(e);
    document.head.appendChild(s);
  });
  return razorpayScriptPromise;
}

async function doUpgrade() {
  if (typeof isSignedIn !== "function" || !isSignedIn()) {
    if (window.Clerk) window.Clerk.openSignIn({});
    return;
  }

  try {
    await loadRazorpayScript();
    const order = await api("/api/payments/create-order", { method: "POST" });

    const rzp = new Razorpay({
      key: order.key_id,
      amount: order.amount,
      currency: order.currency,
      order_id: order.order_id,
      name: "Phoenix Prep",
      description: "Pro membership -- ₹199/mo",
      prefill: { email: (typeof currentUserEmail === "function" && currentUserEmail()) || "" },
      theme: { color: "#4f8cff" },
      handler: async (response) => {
        try {
          await api("/api/payments/verify", {
            method: "POST",
            body: JSON.stringify({
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            }),
          });
          await refreshIdentityDependentState();
        } catch (e) {
          alert(`Payment went through but we couldn't verify it (${e.message}). Contact support with payment id ${response.razorpay_payment_id}.`);
        }
      },
    });
    rzp.open();
  } catch (e) {
    alert(`Couldn't start checkout: ${e.message}`);
  }
}

async function resetProgress() {
  if (!confirm("Reset all solved-problem progress? This can't be undone.")) return;
  await api("/api/submissions", { method: "DELETE" });
  const problemsRes = await api("/api/problems");
  allProblems = problemsRes.problems;
  renderProblemList();
}

function pillClass(difficulty) {
  return `pill ${difficulty}`;
}

function renderProblemList() {
  const diff = document.getElementById("difficultyFilter").value;
  const tag = document.getElementById("tagFilter").value;
  const access = document.getElementById("accessFilter").value;
  const solved = document.getElementById("solvedFilter").value;
  const list = document.getElementById("problemList");
  list.innerHTML = "";

  const filtered = allProblems.filter(p =>
    (!diff || p.difficulty === diff)
    && (!tag || p.tags.includes(tag))
    && (!access || (access === "free" ? p.is_free : !p.is_free))
    && (!solved || (solved === "solved" ? p.solved : !p.solved))
  );

  for (const p of filtered) {
    const li = document.createElement("li");
    li.className = "problem-item"
      + (currentProblem && currentProblem.id === p.id ? " active" : "")
      + (p.locked ? " locked" : "");
    li.innerHTML = `
      <div class="title">${p.solved ? "✅ " : ""}${p.title}</div>
      <div class="meta">
        <span class="${pillClass(p.difficulty)}">${p.difficulty}</span>
        ${p.locked ? `<span class="pill locked-pill">🔒 Pro</span>` : ""}
        ${p.tags.map(t => `<span class="pill tag-pill">${t}</span>`).join("")}
      </div>
    `;
    li.onclick = () => p.locked ? showUpsell() : loadProblem(p.id);
    list.appendChild(li);
  }
}

function populateTagFilter() {
  const tagSet = new Set();
  allProblems.forEach(p => p.tags.forEach(t => tagSet.add(t)));
  const select = document.getElementById("tagFilter");
  [...tagSet].sort().forEach(t => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    select.appendChild(opt);
  });
}

function renderTable(name, table) {
  const rows = table.rows.map(row => `
    <tr>${row.map(v => `<td class="${v === null ? "null-val" : ""}">${v === null ? "NULL" : escapeHtml(v)}</td>`).join("")}</tr>
  `).join("");
  return `
    <div class="sample-table-wrap">
      <h4>${name}</h4>
      <table class="data-table">
        <thead><tr>${table.columns.map(c => `<th>${c}</th>`).join("")}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showUpsell() {
  document.getElementById("workspace").innerHTML = `
    <div class="empty-state upsell-box">
      This problem is part of Pro. Upgrade to ₹199/mo to unlock every practice problem.
      <br/><button id="inlineUpgradeBtn">Upgrade now</button>
    </div>
  `;
  document.getElementById("inlineUpgradeBtn").onclick = doUpgrade;
}

async function loadProblem(id) {
  let p;
  try {
    p = await api(`/api/problems/${id}`);
  } catch (e) {
    if (e.status === 402) return showUpsell();
    throw e;
  }
  currentProblem = p;
  followupState = null;
  renderProblemList();

  const tablesHtml = Object.entries(p.sample_tables)
    .map(([name, table]) => renderTable(name, table))
    .join("");

  document.getElementById("workspace").innerHTML = `
    <div class="problem-header">
      <h2>${p.title} <span class="pill ${p.difficulty}">${p.difficulty}</span></h2>
      <p>${escapeHtml(p.description)}</p>
    </div>
    <div class="tables-section">
      <h3>Schema</h3>
      <div class="schema-block">${escapeHtml(p.schema_sql)}</div>
      <h3>Sample Data</h3>
      ${tablesHtml}
    </div>
    <div class="editor-section">
      <div class="editor-toolbar">
        <strong>Your Query</strong>
        <div class="actions">
          <button class="run-btn" id="runBtn">Run</button>
          <button class="submit-btn" id="submitBtn">Submit</button>
        </div>
      </div>
      <div id="editor"></div>
    </div>
    <div class="results-section" id="resultsSection"></div>
  `;

  mountEditor();
  document.getElementById("runBtn").onclick = () => runQuery(false);
  document.getElementById("submitBtn").onclick = () => runQuery(true);
}

function mountEditor() {
  require.config({ paths: { vs: "https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.47.0/min/vs" } });
  require(["vs/editor/editor.main"], function () {
    if (monacoEditor) monacoEditor.dispose();
    monacoEditor = monaco.editor.create(document.getElementById("editor"), {
      value: "SELECT\n  *\nFROM ",
      language: "sql",
      theme: "vs-dark",
      minimap: { enabled: false },
      fontSize: 13,
      automaticLayout: true,
    });
  });
}

function renderPreviewTable(preview) {
  if (!preview) return "";
  const rows = preview.rows.map(row => `
    <tr>${row.map(v => `<td class="${v === null ? "null-val" : ""}">${v === null ? "NULL" : escapeHtml(v)}</td>`).join("")}</tr>
  `).join("");
  return `
    <table class="data-table">
      <thead><tr>${preview.columns.map(c => `<th>${c}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function runQuery(isSubmit) {
  const query = monacoEditor.getValue();
  const resultsSection = document.getElementById("resultsSection");
  resultsSection.innerHTML = `<div class="loading-dots">Running against DuckDB…</div>`;
  followupState = null;

  const runBtn = document.getElementById("runBtn");
  const submitBtn = document.getElementById("submitBtn");
  runBtn.disabled = true;
  submitBtn.disabled = true;

  try {
    const result = await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({
        problem_id: currentProblem.id,
        query,
        want_explanation: getTutorEnabled(),
      }),
    });
    renderResult(result, isSubmit, query);
    if (result.correct) {
      const problemsRes = await api("/api/problems");
      allProblems = problemsRes.problems;
      renderProblemList();
    }
  } catch (e) {
    if (e.status === 429) {
      resultsSection.innerHTML = `<div class="result-banner fail">⚠️ ${escapeHtml(e.message)}</div>`;
    } else if (e.status === 402) {
      resultsSection.innerHTML = `<div class="upsell-box">
        This problem is part of Pro. Upgrade to ₹199/mo to unlock every practice problem.
        <br/><button id="inlineUpgradeBtn">Upgrade now</button>
      </div>`;
      document.getElementById("inlineUpgradeBtn").onclick = doUpgrade;
    } else {
      resultsSection.innerHTML = `<div class="result-banner fail">Error: ${escapeHtml(e.message)}</div>`;
    }
  } finally {
    runBtn.disabled = false;
    submitBtn.disabled = false;
    refreshTierBadge();
  }
}

function renderResult(result, isSubmit, studentQuery) {
  const resultsSection = document.getElementById("resultsSection");
  const tutorOn = getTutorEnabled();
  let html = "";

  if (result.correct) {
    html += `<div class="result-banner pass">✅ Correct! Verified against DuckDB.</div>`;
  } else {
    html += `<div class="result-banner fail">❌ ${escapeHtml(result.error || "Not quite right.")}</div>`;

    if (result.expected_preview || result.actual_preview) {
      html += `<div class="diff-preview">
        <div class="col"><h4>Expected (preview)</h4>${result.expected_preview ? renderPreviewTable(result.expected_preview) : "—"}</div>
        <div class="col"><h4>Your output (preview)</h4>${result.actual_preview ? renderPreviewTable(result.actual_preview) : "—"}</div>
      </div>`;
    }

    if (!tutorOn) {
      // Tutor toggled off client-side -- backend already skipped the LLM call.
    } else if (result.explanation) {
      html += `<div class="explanation-box"><div class="label">Explain</div><div id="explanationText">${escapeHtml(result.explanation)}</div></div>`;
      html += `<div id="followupThread" class="followup-thread"></div>
        <div class="followup-input-row">
          <input type="text" id="followupInput" placeholder="Ask a follow-up question about this problem…" />
          <button id="followupSendBtn">Ask</button>
        </div>`;
      followupState = {
        studentQuery,
        expectedPreview: result.expected_preview,
        actualPreview: result.actual_preview,
        error: result.actual_preview ? null : result.error,
        conversation: [{ role: "assistant", content: result.explanation }],
      };
    } else if (result.explanation_error) {
      html += `<div class="explanation-box"><div class="label">Explain</div>${escapeHtml(result.explanation_error)}</div>`;
    } else if (!result.explanation_available) {
      html += `<div class="upsell-box">
        You've used today's free explanations. Upgrade to Pro (₹199/mo) for unlimited help.
        <br/><button id="inlineUpgradeBtn">Upgrade now</button>
      </div>`;
    }
  }

  resultsSection.innerHTML = html;
  const upBtn = document.getElementById("inlineUpgradeBtn");
  if (upBtn) upBtn.onclick = doUpgrade;

  const sendBtn = document.getElementById("followupSendBtn");
  const followupInput = document.getElementById("followupInput");
  if (sendBtn && followupInput) {
    sendBtn.onclick = sendFollowup;
    followupInput.onkeydown = (e) => { if (e.key === "Enter") sendFollowup(); };
  }
}

function renderFollowupThread() {
  const thread = document.getElementById("followupThread");
  if (!thread || !followupState) return;
  // Skip turn 0 (the initial explanation) -- it's already shown above in the explanation box.
  const turns = followupState.conversation.slice(1);
  thread.innerHTML = turns.map(t => `
    <div class="followup-turn ${t.role}">
      <div class="who">${t.role === "user" ? "You" : "Explain"}</div>
      ${escapeHtml(t.content)}
    </div>
  `).join("");
}

async function sendFollowup() {
  const input = document.getElementById("followupInput");
  const question = input.value.trim();
  if (!question || !followupState) return;

  const sendBtn = document.getElementById("followupSendBtn");
  sendBtn.disabled = true;
  input.disabled = true;

  followupState.conversation.push({ role: "user", content: question });
  renderFollowupThread();
  input.value = "";

  try {
    const res = await api("/api/ask-followup", {
      method: "POST",
      body: JSON.stringify({
        problem_id: currentProblem.id,
        student_query: followupState.studentQuery,
        expected_preview: followupState.expectedPreview,
        actual_preview: followupState.actualPreview,
        error: followupState.error,
        conversation: followupState.conversation.slice(0, -1),
        question,
      }),
    });
    followupState.conversation.push({ role: "assistant", content: res.answer });
    renderFollowupThread();
  } catch (e) {
    followupState.conversation.pop(); // remove the question we optimistically added
    renderFollowupThread();
    const thread = document.getElementById("followupThread");
    thread.innerHTML += `<div class="result-banner fail">⚠️ ${escapeHtml(e.message)}</div>`;
  } finally {
    sendBtn.disabled = false;
    input.disabled = false;
    refreshTierBadge();
  }
}

let _wasSignedIn = false;

async function refreshIdentityDependentState() {
  // Re-pulls anything keyed off "who is this" -- tier, locked/solved status
  // -- since the answer can change out from under the initial page load
  // (Clerk finishes loading after our first render) or at runtime (sign
  // in/out, an upgrade completing).
  const nowSignedIn = typeof isSignedIn === "function" && isSignedIn();
  if (nowSignedIn && !_wasSignedIn) {
    // Just transitioned into signed-in -- fold whatever solving happened
    // anonymously on this browser (under the localStorage id that never
    // changes) into the real account before it looks like a blank slate.
    try {
      await api("/api/merge-progress", {
        method: "POST",
        body: JSON.stringify({ anonymous_user_id: USER_ID }),
      });
    } catch (e) {
      console.error("[merge-progress] failed:", e);
    }
  }
  _wasSignedIn = nowSignedIn;
  updateSignInGatedUI();

  const problemsRes = await api("/api/problems");
  allProblems = problemsRes.problems;
  renderProblemList();
  await refreshTierBadge();
}

async function init() {
  const problemsRes = await api("/api/problems");
  allProblems = problemsRes.problems;
  await refreshTierBadge();
  populateTagFilter();
  renderProblemList();

  // The very first api() calls above race Clerk's async script load --
  // isSignedIn() is almost always still false at that instant even for a
  // signed-in user, so that first render silently uses the anonymous
  // identity instead. Once Clerk actually finishes loading (and on every
  // subsequent sign-in/out), refresh so tier/locked/solved reflect who's
  // really signed in.
  if (typeof waitForClerk === "function") {
    waitForClerk()
      .then((Clerk) => {
        refreshIdentityDependentState();
        Clerk.addListener(() => refreshIdentityDependentState());
      })
      .catch(() => {
        // Clerk failed to load -- practice mode still works anonymously,
        // nothing further to do here.
      });
  }

  document.getElementById("difficultyFilter").onchange = renderProblemList;
  document.getElementById("tagFilter").onchange = renderProblemList;
  document.getElementById("accessFilter").onchange = renderProblemList;
  document.getElementById("solvedFilter").onchange = renderProblemList;

  const tutorToggle = document.getElementById("tutorToggle");
  tutorToggle.checked = getTutorEnabled();
  tutorToggle.onchange = () => setTutorEnabled(tutorToggle.checked);

  document.getElementById("resetProgressBtn").onclick = resetProgress;

  const mobileFiltersToggle = document.getElementById("mobileFiltersToggle");
  mobileFiltersToggle.onclick = () => {
    document.getElementById("filtersPanel").classList.toggle("mobile-open");
    mobileFiltersToggle.classList.toggle("open");
  };

  document.getElementById("brandHome").onclick = showHome;
  document.getElementById("trackSql").onclick = showSqlTrack;
  document.getElementById("trackInterview").onclick = () => {
    showInterviewScreen();
    if (window.renderInterviewSetup) window.renderInterviewSetup();
  };

  showHome();
}

init();
