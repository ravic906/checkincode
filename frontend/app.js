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
let currentTrack = "sql"; // "sql" | "python" -- which track's problem list is showing
let monacoEditor = null;
let currentTier = "free"; // kept in sync by refreshTierBadge(); Ask Phoenix reads this to decide chat-vs-upsell

let _onPracticeScreen = false;

function updateSignInGatedUI() {
  // Reset Progress only makes sense (a) while on the practice screen,
  // where there's actually something to reset, and (b) once signed in --
  // an anonymous browser id isn't a real account, so offering it before
  // sign-in is just confusing.
  const signedIn = typeof isSignedIn === "function" && isSignedIn();
  document.getElementById("resetProgressBtn").style.display =
    (_onPracticeScreen && signedIn) ? "inline-block" : "none";
}

function updateAskPhoenixFabVisibility() {
  // Only makes sense while an actual problem is loaded and gradeable --
  // hidden on the home/interview screens and on the bare practice-screen
  // empty state.
  document.getElementById("askPhoenixFab").style.display =
    (_onPracticeScreen && currentProblem) ? "flex" : "none";
}

// Keeps ?track=&problem= in the URL in sync with what's on screen, purely
// so a page refresh lands back where you were instead of always resetting
// to the home screen (there's no other client-side routing in this
// no-build-step frontend, so this is deliberately just the URL, not a
// router library).
function syncUrl(params) {
  const url = new URL(window.location.href);
  url.search = "";
  for (const [k, v] of Object.entries(params)) {
    if (v) url.searchParams.set(k, v);
  }
  history.replaceState(null, "", url);
}

function showHome() {
  document.getElementById("homeScreen").style.display = "flex";
  document.getElementById("practiceLayout").style.display = "none";
  document.getElementById("interviewScreen").style.display = "none";
  _onPracticeScreen = false;
  updateSignInGatedUI();
  updateAskPhoenixFabVisibility();
  closeAskPhoenix();
  if (window.stopInterviewAudio) window.stopInterviewAudio();
  syncUrl({});
}
function _resetPracticeWorkspace() {
  // Switching tracks (SQL <-> Python) while a problem was loaded would
  // otherwise leave the OTHER track's problem still rendered in the
  // workspace, out of sync with the sidebar's now-different problem list.
  currentProblem = null;
  document.getElementById("workspace").innerHTML = `
    <div class="empty-state">
      <h2>Pick a problem to get started</h2>
      <p>Every answer is verified by actually running it — no guessing whether you're right. Stuck? Ask Phoenix for help, any time.</p>
    </div>
  `;
}
function showSqlTrack() {
  currentTrack = "sql";
  _resetPracticeWorkspace();
  document.getElementById("homeScreen").style.display = "none";
  document.getElementById("practiceLayout").style.display = "flex";
  document.getElementById("interviewScreen").style.display = "none";
  _onPracticeScreen = true;
  updateSignInGatedUI();
  updateAskPhoenixFabVisibility();
  populateTopicFilter();
  populateTagFilter();
  renderProblemList();
  syncUrl({ track: "sql" });
}
function showPythonTrack() {
  currentTrack = "python";
  _resetPracticeWorkspace();
  document.getElementById("homeScreen").style.display = "none";
  document.getElementById("practiceLayout").style.display = "flex";
  document.getElementById("interviewScreen").style.display = "none";
  _onPracticeScreen = true;
  updateSignInGatedUI();
  updateAskPhoenixFabVisibility();
  populateTopicFilter();
  populateTagFilter();
  renderProblemList();
  syncUrl({ track: "python" });
}
function showInterviewScreen() {
  document.getElementById("homeScreen").style.display = "none";
  document.getElementById("practiceLayout").style.display = "none";
  document.getElementById("interviewScreen").style.display = "flex";
  _onPracticeScreen = false;
  updateSignInGatedUI();
  updateAskPhoenixFabVisibility();
  closeAskPhoenix();
}

async function refreshTierBadge() {
  const usage = await api("/api/usage");
  currentTier = usage.tier;
  const badge = document.getElementById("tierBadge");
  badge.classList.toggle("paid", usage.tier === "paid");
  if (usage.tier === "paid") {
    badge.innerHTML = `Pro — full problem library, unlimited Ask Phoenix`;
  } else {
    // The daily submission counter rarely binds in practice -- the
    // free-tier problem lock is the restriction that actually matters,
    // so lead with that instead of burying it behind a counter that
    // looks generous on its own ("0/20 submissions" reads like broad
    // access). Deliberately no exact counts here (bank size and
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

let activeTopicFilter = null; // set via filterProblemsByTopic(), e.g. from a weak-topic pill on the interview feedback screen

function renderTopicFilterBanner() {
  const banner = document.getElementById("topicFilterBanner");
  if (!activeTopicFilter) { banner.style.display = "none"; return; }
  banner.style.display = "flex";
  banner.innerHTML = `<span>Filtered: ${escapeHtml(activeTopicFilter)}</span><button id="clearTopicFilterBtn">Clear ✕</button>`;
  document.getElementById("clearTopicFilterBtn").onclick = () => {
    activeTopicFilter = null;
    document.getElementById("topicFilter").value = "";
    renderProblemList();
  };
}

function filterProblemsByTopic(topicName) {
  activeTopicFilter = topicName;
  showSqlTrack();
  document.getElementById("topicFilter").value = topicName;
  renderProblemList();
}
window.filterProblemsByTopic = filterProblemsByTopic;

function renderProblemList() {
  const diff = document.getElementById("difficultyFilter").value;
  const tag = document.getElementById("tagFilter").value;
  const access = document.getElementById("accessFilter").value;
  const solved = document.getElementById("solvedFilter").value;
  const list = document.getElementById("problemList");
  list.innerHTML = "";
  renderTopicFilterBanner();

  const filtered = allProblems.filter(p =>
    (p.track || "sql") === currentTrack
    && (!diff || p.difficulty === diff)
    && (!tag || p.tags.includes(tag))
    && (!access || (access === "free" ? p.is_free : !p.is_free))
    && (!solved || (solved === "solved" ? p.solved : !p.solved))
    && (!activeTopicFilter || p.topic === activeTopicFilter)
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
  allProblems.filter(p => (p.track || "sql") === currentTrack).forEach(p => p.tags.forEach(t => tagSet.add(t)));
  const select = document.getElementById("tagFilter");
  select.innerHTML = `<option value="">All tags</option>`;
  [...tagSet].sort().forEach(t => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    select.appendChild(opt);
  });
}

// Separate from the tag filter (individual free-form labels like "trap" or
// "grouping") -- this is the problem's actual topic (e.g. "NumPy
// Aggregations & Boolean Masking"), which is what makes pandas/numpy
// problems discoverable as their own category within the Python track
// instead of being scattered through one long flat tag list.
function populateTopicFilter() {
  const topicSet = new Set();
  allProblems.filter(p => (p.track || "sql") === currentTrack).forEach(p => topicSet.add(p.topic));
  const select = document.getElementById("topicFilter");
  select.innerHTML = `<option value="">All topics</option>`;
  [...topicSet].sort().forEach(t => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    select.appendChild(opt);
  });
  select.value = activeTopicFilter || "";
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

// Minimal markdown-to-HTML for chat answers (Ask Phoenix, mock-interview
// feedback text) -- operates on already-escaped text throughout, so
// entities in the model's own output can never become real tags. Handles
// just what a tutoring answer actually uses: headers, bold/italic/inline
// code, fenced code blocks, tables, and ordered/unordered lists. Not a
// full CommonMark implementation on purpose -- this is a chat bubble, not
// a document renderer.
function renderMarkdownInline(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<![*\w])\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
}

// Collects a possibly-"loose" list starting at line `i`: items can contain
// trailing paragraphs and fenced code blocks (common in real model output --
// e.g. a numbered step followed by an explanation and a code snippet before
// the next number), not just a single line each. Without this, each
// marker line separated by other content became its own one-item list,
// so every step displayed as "1." instead of counting up.
function collectListItems(lines, i, markerRe) {
  const itemsRaw = [];
  let current = null;
  while (i < lines.length) {
    const line = lines[i];
    if (markerRe.test(line)) {
      if (current) itemsRaw.push(current);
      current = [line.replace(markerRe, "")];
      i++;
      continue;
    }
    if (/^```/.test(line.trim())) {
      const fence = [line];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        fence.push(lines[i]);
        i++;
      }
      if (i < lines.length) { fence.push(lines[i]); i++; }
      if (current) current.push(...fence);
      continue;
    }
    const isHeader = /^#{1,4}\s/.test(line);
    const isTableStart = /^\s*\|.*\|\s*$/.test(line) && lines[i + 1] && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]);
    if (isHeader || isTableStart) break;

    if (line.trim() === "") {
      let j = i + 1;
      while (j < lines.length && lines[j].trim() === "") j++;
      if (j >= lines.length || /^#{1,4}\s/.test(lines[j])) { i = j; break; }
      if (markerRe.test(lines[j]) || /^```/.test(lines[j].trim())) {
        // List (or a code block belonging to the current item) resumes
        // right after the gap -- skip the blank lines and let the next
        // loop iteration handle the marker/fence normally.
        i = j;
        continue;
      }
      // Some other content follows (prose, or a different list type).
      // Only treat it as a continuation of the CURRENT item if the list
      // resumes again afterward -- otherwise this is content that comes
      // AFTER the list ends, and must not get swallowed into the last
      // item just because a blank line preceded it.
      let k = j;
      while (k < lines.length && lines[k].trim() !== "" && !markerRe.test(lines[k]) && !/^```/.test(lines[k].trim()) && !/^#{1,4}\s/.test(lines[k])) k++;
      let m = k;
      while (m < lines.length && lines[m].trim() === "") m++;
      if (current && m < lines.length && markerRe.test(lines[m])) {
        current.push("", ...lines.slice(j, k));
        i = k;
        continue;
      }
      i = j;
      break;
    }

    if (!current) break; // non-blank, non-marker line with no active item -- not part of this list
    current.push(line);
    i++;
  }
  if (current) itemsRaw.push(current);
  return { itemsRaw, nextIndex: i };
}

function renderMarkdown(text) {
  const lines = String(text).replace(/\r\n/g, "\n").split("\n");
  const htmlBlocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line.trim())) {
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        code.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      htmlBlocks.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    const headerMatch = line.match(/^(#{1,4})\s+(.*)/);
    if (headerMatch) {
      const level = Math.min(headerMatch[1].length + 3, 6); // ## -> h5, ### -> h6, keeps chat-scale
      htmlBlocks.push(`<h${level}>${renderMarkdownInline(headerMatch[2])}</h${level}>`);
      i++;
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && lines[i + 1] && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const parseRow = row => row.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const headerCells = parseRow(line);
      i += 2; // header + separator
      const bodyRows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        bodyRows.push(parseRow(lines[i]));
        i++;
      }
      htmlBlocks.push(
        "<table><thead><tr>" +
        headerCells.map(c => `<th>${renderMarkdownInline(c)}</th>`).join("") +
        "</tr></thead><tbody>" +
        bodyRows.map(r => "<tr>" + r.map(c => `<td>${renderMarkdownInline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table>"
      );
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const markerRe = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
      const { itemsRaw, nextIndex } = collectListItems(lines, i, markerRe);
      const itemsHtml = itemsRaw.map(raw => {
        // Single-line items render inline (keeps simple bullets compact);
        // items with continuation content (a paragraph or code block that
        // followed on the next lines, before the next marker) render
        // through the full block renderer so that content isn't lost.
        const body = raw.join("\n").trim();
        return raw.length === 1
          ? `<li>${renderMarkdownInline(raw[0])}</li>`
          : `<li>${renderMarkdown(body)}</li>`;
      }).join("");
      htmlBlocks.push(ordered ? `<ol>${itemsHtml}</ol>` : `<ul>${itemsHtml}</ul>`);
      i = nextIndex;
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() !== "" && !/^(```|#{1,4}\s|\s*[-*+]\s|\s*\d+\.\s|\s*\|.*\|\s*$)/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    htmlBlocks.push(`<p>${renderMarkdownInline(para.join(" "))}</p>`);
  }

  return htmlBlocks.join("");
}

function showUpsell() {
  currentProblem = null;
  updateAskPhoenixFabVisibility();
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
  askPhoenixConversation = [];
  renderProblemList();
  updateAskPhoenixFabVisibility();
  syncUrl({ track: currentTrack, problem: id });

  if (p.track === "python") {
    document.getElementById("workspace").innerHTML = `
      <div class="problem-header">
        <h2>${p.title} <span class="pill ${p.difficulty}">${p.difficulty}</span></h2>
        <p>${escapeHtml(p.description)}</p>
      </div>
      <div class="editor-section">
        <div class="editor-toolbar">
          <strong>Your Code</strong>
          <div class="actions">
            <button class="run-btn" id="runBtn">Run</button>
            <button class="submit-btn" id="submitBtn">Submit</button>
          </div>
        </div>
        <div id="editor"></div>
      </div>
      <div class="results-section" id="resultsSection"></div>
    `;
    mountEditor("python", p.starter_code || "");
  } else {
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
    mountEditor("sql", "SELECT\n  *\nFROM ");
  }

  document.getElementById("runBtn").onclick = () => runQuery(false);
  document.getElementById("submitBtn").onclick = () => runQuery(true);
}

function mountEditor(language, seedValue) {
  require.config({ paths: { vs: "https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.47.0/min/vs" } });
  require(["vs/editor/editor.main"], function () {
    if (monacoEditor) monacoEditor.dispose();
    monacoEditor = monaco.editor.create(document.getElementById("editor"), {
      value: seedValue,
      language: language,
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
  const isPython = currentProblem.track === "python";
  const resultsSection = document.getElementById("resultsSection");
  resultsSection.innerHTML = `<div class="loading-dots">${isPython ? "Running in the sandbox…" : "Running against DuckDB…"}</div>`;

  const runBtn = document.getElementById("runBtn");
  const submitBtn = document.getElementById("submitBtn");
  runBtn.disabled = true;
  submitBtn.disabled = true;

  try {
    const result = await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({ problem_id: currentProblem.id, query }),
    });
    if (isPython) renderPythonResult(result); else renderResult(result);
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

function renderResult(result) {
  const resultsSection = document.getElementById("resultsSection");
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

    html += `<p class="ask-phoenix-hint">Stuck? Tap <strong>Ask Phoenix</strong> for help with this problem.</p>`;
  }

  resultsSection.innerHTML = html;
  const upBtn = document.getElementById("inlineUpgradeBtn");
  if (upBtn) upBtn.onclick = doUpgrade;
}

function renderPythonResult(result) {
  const resultsSection = document.getElementById("resultsSection");
  let html = "";

  if (result.correct) {
    html += `<div class="result-banner pass">✅ Correct! All tests passed.</div>`;
  } else {
    html += `<div class="result-banner fail">❌ ${escapeHtml(result.error || "Not quite right.")}</div>`;
    if (result.output) {
      html += `<div class="schema-block">${escapeHtml(result.output)}</div>`;
    }
    html += `<p class="ask-phoenix-hint">Stuck? Tap <strong>Ask Phoenix</strong> for help with this problem.</p>`;
  }

  resultsSection.innerHTML = html;
  const upBtn = document.getElementById("inlineUpgradeBtn");
  if (upBtn) upBtn.onclick = doUpgrade;
}

// -------- Ask Phoenix: open-ended contextual help, any time a problem is loaded --------

let askPhoenixConversation = []; // [{role: "user"|"assistant", content}], reset whenever the loaded problem changes

function openAskPhoenix() {
  document.getElementById("askPhoenixOverlay").style.display = "flex";
  document.getElementById("askPhoenixSubtitle").textContent = currentProblem ? currentProblem.title : "";
  renderAskPhoenixBody();
}

function closeAskPhoenix() {
  const overlay = document.getElementById("askPhoenixOverlay");
  if (overlay) overlay.style.display = "none";
}

function renderAskPhoenixBody() {
  const body = document.getElementById("askPhoenixBody");
  if (currentTier !== "paid") {
    body.innerHTML = `
      <div class="upsell-box">
        Ask Phoenix is a Pro feature -- get contextual AI help on any problem, any time, not just after a wrong answer.
        Upgrade to ₹199/mo to unlock it.
        <br/><button id="askPhoenixUpgradeBtn">Upgrade now</button>
      </div>
    `;
    document.getElementById("askPhoenixUpgradeBtn").onclick = doUpgrade;
    return;
  }

  body.innerHTML = `
    <div class="ask-phoenix-thread" id="askPhoenixThread"></div>
    <div class="ask-phoenix-input-row">
      <input type="text" id="askPhoenixInput" placeholder="Ask about this problem…" />
      <button id="askPhoenixSendBtn">Ask</button>
    </div>
  `;
  renderAskPhoenixThread();

  const input = document.getElementById("askPhoenixInput");
  document.getElementById("askPhoenixSendBtn").onclick = sendAskPhoenixMessage;
  input.onkeydown = (e) => { if (e.key === "Enter") sendAskPhoenixMessage(); };
  input.focus();
}

function renderAskPhoenixThread() {
  const thread = document.getElementById("askPhoenixThread");
  if (!thread) return;
  if (askPhoenixConversation.length === 0) {
    thread.innerHTML = `<p class="ask-phoenix-empty">Ask anything about this problem -- how to approach it, what a concept means, or why your in-progress query might be off.</p>`;
    return;
  }
  thread.innerHTML = askPhoenixConversation.map(t => `
    <div class="followup-turn ${t.role}">
      <div class="who">${t.role === "user" ? "You" : "Phoenix"}</div>
      ${t.role === "user" ? `<p>${escapeHtml(t.content)}</p>` : renderMarkdown(t.content)}
    </div>
  `).join("");
  thread.scrollTop = thread.scrollHeight;
}

async function sendAskPhoenixMessage() {
  const input = document.getElementById("askPhoenixInput");
  const question = input.value.trim();
  if (!question) return;

  const sendBtn = document.getElementById("askPhoenixSendBtn");
  sendBtn.disabled = true;
  input.disabled = true;

  askPhoenixConversation.push({ role: "user", content: question });
  renderAskPhoenixThread();
  input.value = "";

  try {
    const res = await api("/api/ask-phoenix", {
      method: "POST",
      body: JSON.stringify({
        problem_id: currentProblem.id,
        current_query: monacoEditor ? monacoEditor.getValue() : null,
        conversation: askPhoenixConversation.slice(0, -1),
        question,
      }),
    });
    askPhoenixConversation.push({ role: "assistant", content: res.answer });
    renderAskPhoenixThread();
  } catch (e) {
    askPhoenixConversation.pop(); // remove the question we optimistically added
    renderAskPhoenixThread();
    const thread = document.getElementById("askPhoenixThread");
    thread.innerHTML += `<div class="result-banner fail">⚠️ ${escapeHtml(e.message)}</div>`;
  } finally {
    sendBtn.disabled = false;
    input.disabled = false;
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
  populateTopicFilter();
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
  document.getElementById("topicFilter").onchange = () => {
    activeTopicFilter = document.getElementById("topicFilter").value || null;
    renderProblemList();
  };
  document.getElementById("tagFilter").onchange = renderProblemList;
  document.getElementById("accessFilter").onchange = renderProblemList;
  document.getElementById("solvedFilter").onchange = renderProblemList;

  document.getElementById("resetProgressBtn").onclick = resetProgress;

  const mobileFiltersToggle = document.getElementById("mobileFiltersToggle");
  mobileFiltersToggle.onclick = () => {
    document.getElementById("filtersPanel").classList.toggle("mobile-open");
    mobileFiltersToggle.classList.toggle("open");
  };

  document.getElementById("askPhoenixFab").onclick = openAskPhoenix;
  document.getElementById("askPhoenixClose").onclick = closeAskPhoenix;
  document.getElementById("askPhoenixOverlay").onclick = (e) => {
    if (e.target.id === "askPhoenixOverlay") closeAskPhoenix();
  };

  document.getElementById("brandHome").onclick = showHome;
  document.getElementById("trackSql").onclick = showSqlTrack;
  document.getElementById("trackPython").onclick = showPythonTrack;
  document.getElementById("trackInterview").onclick = () => {
    showInterviewScreen();
    if (window.renderInterviewSetup) window.renderInterviewSetup();
  };

  // Restore whatever track/problem was in the URL (see syncUrl()) so a
  // page refresh lands back where you were instead of always resetting
  // to the home screen.
  const restoreParams = new URLSearchParams(window.location.search);
  const savedTrack = restoreParams.get("track");
  const savedProblem = restoreParams.get("problem");
  if (savedTrack === "python") {
    showPythonTrack();
  } else if (savedTrack === "sql") {
    showSqlTrack();
  } else {
    showHome();
  }
  if (savedProblem) {
    await loadProblem(savedProblem);
  }
}

init();
