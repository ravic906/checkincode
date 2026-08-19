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
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": USER_ID,
      ...(options.headers || {}),
    },
  });
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

async function refreshTierBadge() {
  const usage = await api("/api/usage");
  const badge = document.getElementById("tierBadge");
  badge.classList.toggle("paid", usage.tier === "paid");
  if (usage.tier === "paid") {
    badge.innerHTML = `Pro — unlimited AI explanations`;
  } else {
    badge.innerHTML = `Free — ${usage.explanations_today}/${usage.free_daily_explanations} AI explanations today, ${usage.submissions_today}/${usage.free_daily_submissions} submissions <button id="upgradeBtn">Upgrade ₹199/mo</button>`;
  }
  const btn = document.getElementById("upgradeBtn");
  if (btn) btn.onclick = doUpgrade;
  return usage;
}

async function doUpgrade() {
  await api("/api/dev/upgrade", { method: "POST" });
  await refreshTierBadge();
}

function pillClass(difficulty) {
  return `pill ${difficulty}`;
}

function renderProblemList() {
  const diff = document.getElementById("difficultyFilter").value;
  const tag = document.getElementById("tagFilter").value;
  const list = document.getElementById("problemList");
  list.innerHTML = "";

  const filtered = allProblems.filter(p =>
    (!diff || p.difficulty === diff) && (!tag || p.tags.includes(tag))
  );

  for (const p of filtered) {
    const li = document.createElement("li");
    li.className = "problem-item" + (currentProblem && currentProblem.id === p.id ? " active" : "");
    li.innerHTML = `
      <div class="title">${p.title}</div>
      <div class="meta">
        <span class="${pillClass(p.difficulty)}">${p.difficulty}</span>
        ${p.tags.map(t => `<span class="pill tag-pill">${t}</span>`).join("")}
      </div>
    `;
    li.onclick = () => loadProblem(p.id);
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

async function loadProblem(id) {
  const p = await api(`/api/problems/${id}`);
  currentProblem = p;
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

  const runBtn = document.getElementById("runBtn");
  const submitBtn = document.getElementById("submitBtn");
  runBtn.disabled = true;
  submitBtn.disabled = true;

  try {
    const result = await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({ problem_id: currentProblem.id, query }),
    });
    renderResult(result, isSubmit);
  } catch (e) {
    if (e.status === 429) {
      resultsSection.innerHTML = `<div class="result-banner fail">⚠️ ${escapeHtml(e.message)}</div>`;
    } else {
      resultsSection.innerHTML = `<div class="result-banner fail">Error: ${escapeHtml(e.message)}</div>`;
    }
  } finally {
    runBtn.disabled = false;
    submitBtn.disabled = false;
    refreshTierBadge();
  }
}

function renderResult(result, isSubmit) {
  const resultsSection = document.getElementById("resultsSection");
  let html = "";

  if (result.correct) {
    html += `<div class="result-banner pass">✅ Correct! Verified against DuckDB — no AI call needed.</div>`;
  } else {
    html += `<div class="result-banner fail">❌ ${escapeHtml(result.error || "Not quite right.")}</div>`;

    if (result.expected_preview || result.actual_preview) {
      html += `<div class="diff-preview">
        <div class="col"><h4>Expected (preview)</h4>${result.expected_preview ? renderPreviewTable(result.expected_preview) : "—"}</div>
        <div class="col"><h4>Your output (preview)</h4>${result.actual_preview ? renderPreviewTable(result.actual_preview) : "—"}</div>
      </div>`;
    }

    if (result.explanation) {
      html += `<div class="explanation-box"><div class="label">AI Tutor</div>${escapeHtml(result.explanation)}</div>`;
    } else if (result.explanation_error) {
      html += `<div class="explanation-box"><div class="label">AI Tutor</div>${escapeHtml(result.explanation_error)}</div>`;
    } else if (!result.explanation_available) {
      html += `<div class="upsell-box">
        You've used today's free AI explanations. Upgrade to Pro (₹199/mo) for unlimited explanations on wrong answers.
        <br/><button id="inlineUpgradeBtn">Upgrade now</button>
      </div>`;
    }
  }

  resultsSection.innerHTML = html;
  const upBtn = document.getElementById("inlineUpgradeBtn");
  if (upBtn) upBtn.onclick = doUpgrade;
}

async function init() {
  const [problemsRes] = await Promise.all([api("/api/problems"), refreshTierBadge()]);
  allProblems = problemsRes.problems;
  populateTagFilter();
  renderProblemList();

  document.getElementById("difficultyFilter").onchange = renderProblemList;
  document.getElementById("tagFilter").onchange = renderProblemList;
}

init();
