/*
 * Not linked from the main nav for non-admins -- reached at /admin.html.
 * Two ways in, both accepted by the backend's _require_admin(): a
 * signed-in Clerk account with is_admin=True, or the shared X-Admin-Token
 * secret kept as a bootstrap/fallback path (see admin-shared.js).
 */

initAdminSidebar("problems");

function renderDraft(p) {
  return `
    <div class="draft-card" id="draft-${p.id}">
      <h3>${escapeHtml(p.title)}</h3>
      <div class="draft-meta">${escapeHtml(p.difficulty)} · ${escapeHtml(p.topic)} · ${escapeHtml((p.tags || []).join(", "))}</div>
      <div>${escapeHtml(p.description)}</div>
      <div class="schema-block">${escapeHtml(p.schema_sql.trim())}</div>
      <div class="schema-block">${escapeHtml(p.seed_sql.trim())}</div>
      <div class="schema-block">${escapeHtml(p.canonical_sql.trim())}</div>
      <div class="draft-actions">
        <button class="btn btn-success btn-sm" onclick="approveDraft('${p.id}')">Approve</button>
        <button class="btn btn-danger btn-sm" onclick="rejectDraft('${p.id}')">Reject</button>
      </div>
    </div>
  `;
}

async function loadPending() {
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
      : `<p class="empty-note">No pending drafts.</p>`;
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
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
let activeLiveCategory = null; // null | "sql" | "python" | "stats" | "pandas" | "numpy" -- set by clicking a count pill

function renderLiveCategoryCounts() {
  const counts = { sql: 0, python: 0, stats: 0, pandas: 0, numpy: 0, case: 0 };
  for (const p of allLiveProblems) counts[categoryBucket(p.track, p.topic)]++;
  const el = document.getElementById("liveCategoryCounts");
  el.innerHTML = Object.entries(CATEGORY_META).map(([key, meta]) => `
    <button class="category-pill${activeLiveCategory === key ? " active" : ""}" data-category="${key}">
      ${meta.label} <span class="count">${counts[key]}</span>
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
    <div class="live-row" id="live-${p.id}">
      <div>
        <div class="title">${escapeHtml(p.title)}</div>
        <div class="meta">${escapeHtml(p.track)} · ${escapeHtml(p.difficulty)} · ${escapeHtml(p.topic)} · ${escapeHtml((p.tags || []).join(", "))}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="unpublishProblem('${p.id}')">Unpublish</button>
    </div>
  `;
}

function applyLiveFilters() {
  const q = document.getElementById("liveSearch").value.trim().toLowerCase();
  const track = document.getElementById("liveTrackFilter").value;
  const filtered = allLiveProblems.filter(p => {
    if (activeLiveCategory && categoryBucket(p.track, p.topic) !== activeLiveCategory) return false;
    if (track && p.track !== track) return false;
    if (q && !(p.title.toLowerCase().includes(q) || p.topic.toLowerCase().includes(q))) return false;
    return true;
  });
  const listEl = document.getElementById("liveList");
  listEl.innerHTML = filtered.length
    ? filtered.map(renderLiveCard).join("")
    : `<p class="empty-note">No matching live problems.</p>`;
}

async function loadLive() {
  const listEl = document.getElementById("liveList");
  listEl.innerHTML = `<div class="loading-dots">Loading…</div>`;
  try {
    const data = await adminApi("/api/admin/problems/live");
    allLiveProblems = data.problems;
    renderLiveCategoryCounts();
    applyLiveFilters();
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
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

loadPending();
