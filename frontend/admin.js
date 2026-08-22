/*
 * Not linked from the main nav -- reached directly at /admin.html.
 * Two ways in, both accepted by the backend's _require_admin(): the
 * original shared X-Admin-Token secret (kept as a bootstrap/fallback
 * path), or a signed-in Clerk account with is_admin=True. Sign in via
 * the button auth.js renders, then use "Grant myself admin" once (needs
 * the token) -- after that, being signed in is enough on its own.
 */

const ADMIN_API_BASE = window.API_BASE || "http://127.0.0.1:8000";

function getToken() {
  return document.getElementById("adminToken").value.trim() || localStorage.getItem("phoenix_admin_token") || "";
}

async function adminApi(path, options = {}) {
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (token) headers["X-Admin-Token"] = token;
  if (typeof getAuthToken === "function") {
    const clerkToken = await getAuthToken();
    if (clerkToken) headers["Authorization"] = `Bearer ${clerkToken}`;
  }
  const res = await fetch(`${ADMIN_API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

async function grantMyselfAdmin() {
  try {
    const result = await adminApi("/api/admin/grant-admin", { method: "POST" });
    alert(`Done -- ${result.user_id} is now an admin. You can sign in with this account from now on, no token needed.`);
  } catch (err) {
    alert(`Grant admin failed: ${err.message}`);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderDraft(p) {
  return `
    <div class="draft-card" id="draft-${p.id}">
      <h3>${escapeHtml(p.title)}</h3>
      <div class="draft-meta">${escapeHtml(p.difficulty)} · ${escapeHtml(p.topic)} · ${escapeHtml((p.tags || []).join(", "))}</div>
      <div>${escapeHtml(p.description)}</div>
      <div class="schema-block" style="margin-top:10px;">${escapeHtml(p.schema_sql.trim())}</div>
      <div class="schema-block">${escapeHtml(p.seed_sql.trim())}</div>
      <div class="schema-block">${escapeHtml(p.canonical_sql.trim())}</div>
      <div class="draft-actions">
        <button class="approve-btn" onclick="approveDraft('${p.id}')">Approve</button>
        <button class="reject-btn" onclick="rejectDraft('${p.id}')">Reject</button>
      </div>
    </div>
  `;
}

async function loadPending() {
  localStorage.setItem("phoenix_admin_token", getToken());
  const listEl = document.getElementById("pendingList");
  listEl.innerHTML = `<div class="loading-dots">Loading…</div>`;
  try {
    const [pending, cadence] = await Promise.all([
      adminApi("/api/admin/problems/pending"),
      adminApi("/api/admin/cadence"),
    ]);
    document.getElementById("cadenceNote").textContent = cadence.last_batch_generated_at
      ? `Last batch generated: ${new Date(cadence.last_batch_generated_at).toLocaleString()}`
      : "No batch generated yet.";
    listEl.innerHTML = pending.problems.length
      ? pending.problems.map(renderDraft).join("")
      : `<p style="color:var(--muted);">No pending drafts.</p>`;
  } catch (err) {
    listEl.innerHTML = `<div class="result-banner fail">${escapeHtml(err.message)}</div>`;
  }
}

async function approveDraft(id) {
  try {
    await adminApi(`/api/admin/problems/${id}/approve`, { method: "POST" });
    document.getElementById(`draft-${id}`).remove();
  } catch (err) {
    alert(`Approve failed: ${err.message}`);
  }
}

async function rejectDraft(id) {
  try {
    await adminApi(`/api/admin/problems/${id}/reject`, { method: "POST" });
    document.getElementById(`draft-${id}`).remove();
  } catch (err) {
    alert(`Reject failed: ${err.message}`);
  }
}

let allLiveProblems = [];
let activeLiveCategory = null; // null | "sql" | "python" | "pandas" | "numpy" -- set by clicking a count pill, drills the list without needing the track dropdown too

// Same bucketing rule as problems.py's _category_bucket() / admin-users.js's
// categoryBucket() -- pandas/numpy are topic buckets within track='python',
// not their own track.
const LIVE_PANDAS_TOPICS = new Set([
  "Pandas Data Cleaning & Missing Data",
  "Pandas Merging, Joining & Reshaping",
  "Pandas GroupBy & Aggregation",
  "Pandas Time Series Operations",
]);
const LIVE_NUMPY_TOPICS = new Set([
  "NumPy Array Creation & Indexing",
  "NumPy Broadcasting & Vectorization",
  "NumPy Aggregations & Boolean Masking",
]);
function liveCategoryBucket(p) {
  if (p.track !== "python") return "sql";
  if (LIVE_PANDAS_TOPICS.has(p.topic)) return "pandas";
  if (LIVE_NUMPY_TOPICS.has(p.topic)) return "numpy";
  return "python";
}

function renderLiveCategoryCounts() {
  const counts = { sql: 0, python: 0, pandas: 0, numpy: 0 };
  for (const p of allLiveProblems) counts[liveCategoryBucket(p)]++;
  const labels = { sql: "SQL", python: "Python", pandas: "Pandas", numpy: "NumPy" };
  const el = document.getElementById("liveCategoryCounts");
  el.innerHTML = Object.entries(labels).map(([key, label]) => `
    <button class="category-pill${activeLiveCategory === key ? " active" : ""}" data-category="${key}">
      ${label} <span class="category-pill-count">${counts[key]}</span>
    </button>
  `).join("") + (activeLiveCategory ? `<button class="category-pill clear-pill" id="clearLiveCategoryBtn">Clear ✕</button>` : "");
  el.querySelectorAll(".category-pill[data-category]").forEach(btn => {
    btn.onclick = () => {
      activeLiveCategory = btn.dataset.category;
      renderLiveCategoryCounts();
      applyLiveFilters();
    };
  });
  const clearBtn = document.getElementById("clearLiveCategoryBtn");
  if (clearBtn) clearBtn.onclick = () => {
    activeLiveCategory = null;
    renderLiveCategoryCounts();
    applyLiveFilters();
  };
}

function renderLiveCard(p) {
  return `
    <div class="live-card" id="live-${p.id}">
      <div>
        <div>${escapeHtml(p.title)}</div>
        <div class="meta">${escapeHtml(p.track)} · ${escapeHtml(p.difficulty)} · ${escapeHtml(p.topic)} · ${escapeHtml((p.tags || []).join(", "))}</div>
      </div>
      <button class="unpublish-btn" onclick="unpublishProblem('${p.id}')">Unpublish</button>
    </div>
  `;
}

function applyLiveFilters() {
  const q = document.getElementById("liveSearch").value.trim().toLowerCase();
  const track = document.getElementById("liveTrackFilter").value;
  const filtered = allLiveProblems.filter(p => {
    if (activeLiveCategory && liveCategoryBucket(p) !== activeLiveCategory) return false;
    if (track && p.track !== track) return false;
    if (q && !(p.title.toLowerCase().includes(q) || p.topic.toLowerCase().includes(q))) return false;
    return true;
  });
  const listEl = document.getElementById("liveList");
  listEl.innerHTML = filtered.length
    ? filtered.map(renderLiveCard).join("")
    : `<p style="color:var(--muted);">No matching live problems.</p>`;
}

async function loadLive() {
  localStorage.setItem("phoenix_admin_token", getToken());
  const listEl = document.getElementById("liveList");
  listEl.innerHTML = `<div class="loading-dots">Loading…</div>`;
  try {
    const data = await adminApi("/api/admin/problems/live");
    allLiveProblems = data.problems;
    renderLiveCategoryCounts();
    applyLiveFilters();
  } catch (err) {
    listEl.innerHTML = `<div class="result-banner fail">${escapeHtml(err.message)}</div>`;
  }
}

async function unpublishProblem(id) {
  if (!confirm("Unpublish this problem? It will no longer be shown to students (can be republished later if needed).")) return;
  try {
    await adminApi(`/api/admin/problems/${id}/unpublish`, { method: "POST" });
    document.getElementById(`live-${id}`).remove();
    allLiveProblems = allLiveProblems.filter(p => p.id !== id);
  } catch (err) {
    alert(`Unpublish failed: ${err.message}`);
  }
}

document.getElementById("liveSearch").addEventListener("input", applyLiveFilters);
document.getElementById("liveTrackFilter").addEventListener("change", applyLiveFilters);

document.getElementById("tabPending").onclick = () => {
  document.getElementById("tabPending").classList.add("active");
  document.getElementById("tabLive").classList.remove("active");
  document.getElementById("pendingSection").style.display = "";
  document.getElementById("liveSection").style.display = "none";
};
document.getElementById("tabLive").onclick = () => {
  document.getElementById("tabLive").classList.add("active");
  document.getElementById("tabPending").classList.remove("active");
  document.getElementById("liveSection").style.display = "";
  document.getElementById("pendingSection").style.display = "none";
  if (allLiveProblems.length === 0) loadLive();
};

document.getElementById("loadBtn").onclick = () => {
  const onLiveTab = document.getElementById("tabLive").classList.contains("active");
  return onLiveTab ? loadLive() : loadPending();
};
document.getElementById("generateBtn").onclick = async () => {
  const btn = document.getElementById("generateBtn");
  btn.disabled = true;
  btn.textContent = "Generating…";
  try {
    const result = await adminApi("/api/admin/problems/generate-batch", {
      method: "POST",
      body: JSON.stringify({ count: 5 }),
    });
    alert(`Inserted ${result.inserted.length} draft(s), skipped ${result.skipped.length}.`);
    await loadPending();
  } catch (err) {
    alert(`Generate failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate New Batch";
  }
};

document.getElementById("grantAdminBtn").onclick = grantMyselfAdmin;

const savedToken = localStorage.getItem("phoenix_admin_token");
if (savedToken) document.getElementById("adminToken").value = savedToken;
