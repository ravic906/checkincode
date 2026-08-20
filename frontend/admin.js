/*
 * Not linked from the main nav -- reached directly at /admin.html. Not real
 * auth, just a shared secret (ADMIN_TOKEN) checked server-side; treat this
 * URL itself as something not to publicize.
 */

const ADMIN_API_BASE = window.API_BASE || "http://127.0.0.1:8000";

function getToken() {
  return document.getElementById("adminToken").value.trim() || localStorage.getItem("phoenix_admin_token") || "";
}

async function adminApi(path, options = {}) {
  const token = getToken();
  const res = await fetch(`${ADMIN_API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Admin-Token": token,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
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

document.getElementById("loadBtn").onclick = loadPending;
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

const savedToken = localStorage.getItem("phoenix_admin_token");
if (savedToken) document.getElementById("adminToken").value = savedToken;
