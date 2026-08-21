/*
 * Not linked from the main nav -- reached directly at /admin-users.html.
 * Not real auth, just a shared secret (ADMIN_TOKEN) checked server-side;
 * treat this URL itself as something not to publicize.
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

function renderSummary(summary) {
  return `
    <div class="summary-card"><div class="num">${summary.total_users}</div><div class="label">Total Users</div></div>
    <div class="summary-card"><div class="num">${summary.paid_users}</div><div class="label">Pro Users</div></div>
    <div class="summary-card"><div class="num">${summary.free_users}</div><div class="label">Free Users</div></div>
  `;
}

function renderUserRow(u) {
  const label = u.email || u.id;
  const joined = u.created_at ? new Date(u.created_at).toLocaleDateString() : "—";
  return `
    <tr class="user-row" data-user-id="${escapeHtml(u.id)}">
      <td>${escapeHtml(label)}</td>
      <td><span class="tier-pill ${u.tier}">${escapeHtml(u.tier)}</span></td>
      <td>${u.submissions_today}</td>
      <td>${u.total_submissions}</td>
      <td>${u.solved_count}</td>
      <td>${u.interviews_this_month}</td>
      <td>${joined}</td>
    </tr>
  `;
}

function renderHistoryRow(h) {
  const mark = h.correct ? '<span class="pass-mark">✓ pass</span>' : '<span class="fail-mark">✗ fail</span>';
  const when = h.submitted_at ? new Date(h.submitted_at).toLocaleString() : "—";
  const title = h.title || `(deleted problem ${h.problem_id})`;
  const track = h.track ? h.track.toUpperCase() : "";
  return `
    <div class="history-row">
      <span>${escapeHtml(title)} <span style="color:var(--muted);">· ${escapeHtml(track)} · ${escapeHtml(h.topic || "")}</span></span>
      <span>${mark} · ${escapeHtml(when)}</span>
    </div>
  `;
}

let currentOpenUserId = null;

async function toggleHistory(userId, rowEl) {
  const panel = document.getElementById("historyPanel");
  if (currentOpenUserId === userId) {
    panel.classList.remove("open");
    currentOpenUserId = null;
    return;
  }
  currentOpenUserId = userId;
  panel.classList.add("open");
  panel.innerHTML = `<div class="loading-dots">Loading history…</div>`;
  rowEl.parentNode.insertBefore(panel, rowEl.nextSibling);
  try {
    const data = await adminApi(`/api/admin/users/${encodeURIComponent(userId)}/history`);
    panel.innerHTML = data.history.length
      ? data.history.map(renderHistoryRow).join("")
      : `<p style="color:var(--muted);">No submissions yet.</p>`;
  } catch (err) {
    panel.innerHTML = `<div class="result-banner fail">${escapeHtml(err.message)}</div>`;
  }
}

async function loadUsers() {
  localStorage.setItem("phoenix_admin_token", getToken());
  const body = document.getElementById("usersBody");
  const cards = document.getElementById("summaryCards");
  body.innerHTML = `<tr><td colspan="7"><div class="loading-dots">Loading…</div></td></tr>`;
  try {
    const [summary, usersResp] = await Promise.all([
      adminApi("/api/admin/users/summary"),
      adminApi("/api/admin/users"),
    ]);
    cards.innerHTML = renderSummary(summary);
    body.innerHTML = usersResp.users.length
      ? usersResp.users.map(renderUserRow).join("")
      : `<tr><td colspan="7" style="color:var(--muted);">No users yet.</td></tr>`;
    body.querySelectorAll("tr.user-row").forEach(row => {
      row.addEventListener("click", () => toggleHistory(row.dataset.userId, row));
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="7"><div class="result-banner fail">${escapeHtml(err.message)}</div></td></tr>`;
  }
}

document.getElementById("loadBtn").onclick = loadUsers;

const savedToken = localStorage.getItem("phoenix_admin_token");
if (savedToken) document.getElementById("adminToken").value = savedToken;
