/*
 * Not linked from the main nav for non-admins -- reached at
 * /admin-users.html. Accepts either the shared X-Admin-Token secret or a
 * signed-in Clerk account with is_admin=True (see main.py's _require_admin()
 * and admin-shared.js).
 */

initAdminSidebar("users");

function renderSummary(summary) {
  return `
    <div class="stat-card"><div class="num">${summary.total_users}</div><div class="label">Total Users</div></div>
    <div class="stat-card"><div class="num">${summary.paid_users}</div><div class="label">Pro Users</div></div>
    <div class="stat-card"><div class="num">${summary.free_users}</div><div class="label">Free Users</div></div>
  `;
}

function renderUserRow(u) {
  const label = u.email || u.id;
  const joined = u.created_at ? new Date(u.created_at).toLocaleDateString() : "—";
  return `
    <tr class="clickable" data-user-id="${escapeHtml(u.id)}">
      <td>${escapeHtml(label)}</td>
      <td><span class="pill tier-${u.tier}">${escapeHtml(u.tier)}</span></td>
      <td>${u.submissions_today}</td>
      <td>${u.total_submissions}</td>
      <td>${u.solved_count}</td>
      <td>${u.interviews_this_month}</td>
      <td>${joined}</td>
    </tr>
  `;
}

function computeUserBreakdown(history) {
  const solvedProblemIds = new Set();
  const counts = { sql: 0, python: 0, stats: 0, pandas: 0, numpy: 0, case: 0 };
  for (const h of history) {
    if (!h.correct || solvedProblemIds.has(h.problem_id)) continue;
    solvedProblemIds.add(h.problem_id);
    counts[categoryBucket(h.track, h.topic)]++;
  }
  return counts;
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
    panel.innerHTML = "";
    currentOpenUserId = null;
    return;
  }
  currentOpenUserId = userId;
  panel.innerHTML = `<div class="card"><div class="loading-dots">Loading history…</div></div>`;
  try {
    const data = await adminApi(`/api/admin/users/${encodeURIComponent(userId)}/history`);
    const breakdown = computeUserBreakdown(data.history);
    const chartHtml = `<div class="section-title">Solved by category</div>${renderCategoryPie(breakdown, 90)}`;
    const historyHtml = data.history.length
      ? `<div class="history-panel">${data.history.map(renderHistoryRow).join("")}</div>`
      : `<p class="empty-note">No submissions yet.</p>`;
    panel.innerHTML = `<div class="card">${chartHtml}</div>${historyHtml}`;
  } catch (err) {
    panel.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

async function loadUsers() {
  const body = document.getElementById("usersBody");
  const cards = document.getElementById("summaryCards");
  body.innerHTML = `<tr><td colspan="7"><div class="loading-dots">Loading…</div></td></tr>`;
  try {
    const [summary, usersResp, breakdown] = await Promise.all([
      adminApi("/api/admin/users/summary"),
      adminApi("/api/admin/users"),
      adminApi("/api/admin/stats/solved-by-category"),
    ]);
    cards.innerHTML = renderSummary(summary);
    document.getElementById("categoryChart").innerHTML = renderCategoryPie(breakdown, 140);
    body.innerHTML = usersResp.users.length
      ? usersResp.users.map(renderUserRow).join("")
      : `<tr><td colspan="7" class="empty-note">No users yet.</td></tr>`;
    body.querySelectorAll("tr.clickable").forEach(row => {
      row.addEventListener("click", () => toggleHistory(row.dataset.userId, row));
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="7"><div class="error-banner">${escapeHtml(err.message)}</div></td></tr>`;
  }
}

loadUsers();
