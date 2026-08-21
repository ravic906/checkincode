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

// Category order/colors shared by the platform-wide chart and every
// per-user breakdown -- pandas/numpy are topic buckets within
// track='python', not separate tracks (see problems.py's
// _category_bucket), so "category" here means content bucket, not the
// `track` column.
const CATEGORY_META = {
  sql: { label: "SQL", color: "var(--accent)" },
  python: { label: "Python", color: "var(--green)" },
  pandas: { label: "Pandas", color: "var(--amber)" },
  numpy: { label: "NumPy", color: "var(--red)" },
};

// Renders a small SVG pie chart from {sql, python, pandas, numpy} counts,
// plus a text legend with each count. No charting library -- this is a
// no-build-step frontend, and a handful of arcs is simple enough to
// compute directly.
function renderCategoryPie(counts, size = 120) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const r = size / 2;
  const cx = r, cy = r;
  let angle = -Math.PI / 2; // start at 12 o'clock
  let slices = "";
  if (total === 0) {
    slices = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="var(--panel-2)" />`;
  } else {
    for (const [key, meta] of Object.entries(CATEGORY_META)) {
      const value = counts[key] || 0;
      if (value === 0) continue;
      const slice = (value / total) * Math.PI * 2;
      const x1 = cx + r * Math.cos(angle);
      const y1 = cy + r * Math.sin(angle);
      angle += slice;
      const x2 = cx + r * Math.cos(angle);
      const y2 = cy + r * Math.sin(angle);
      const largeArc = slice > Math.PI ? 1 : 0;
      slices += `<path d="M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${largeArc} 1 ${x2},${y2} Z" fill="${meta.color}" />`;
    }
  }
  const legend = Object.entries(CATEGORY_META).map(([key, meta]) => `
    <div class="legend-row">
      <span class="legend-swatch" style="background:${meta.color};"></span>
      <span>${meta.label}</span>
      <span class="legend-count">${counts[key] || 0}</span>
    </div>
  `).join("");
  return `
    <div class="pie-chart-wrap">
      <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="Solved problems by category">${slices}</svg>
      <div class="legend">${legend}</div>
    </div>
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

// Buckets a submission-history row's (track, topic) the same way
// problems.py's _category_bucket does server-side, so the per-user
// drill-down chart matches the platform-wide one's categorization.
const PANDAS_TOPICS = new Set([
  "Pandas Data Cleaning & Missing Data",
  "Pandas Merging, Joining & Reshaping",
  "Pandas GroupBy & Aggregation",
  "Pandas Time Series Operations",
]);
const NUMPY_TOPICS = new Set([
  "NumPy Array Creation & Indexing",
  "NumPy Broadcasting & Vectorization",
  "NumPy Aggregations & Boolean Masking",
]);
function categoryBucket(track, topic) {
  if (track !== "python") return "sql";
  if (PANDAS_TOPICS.has(topic)) return "pandas";
  if (NUMPY_TOPICS.has(topic)) return "numpy";
  return "python";
}

function computeUserBreakdown(history) {
  const solvedProblemIds = new Set();
  const counts = { sql: 0, python: 0, pandas: 0, numpy: 0 };
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
    const breakdown = computeUserBreakdown(data.history);
    const chartHtml = `<div class="user-breakdown-header">Solved by category</div>${renderCategoryPie(breakdown, 90)}`;
    panel.innerHTML = chartHtml + (data.history.length
      ? data.history.map(renderHistoryRow).join("")
      : `<p style="color:var(--muted);">No submissions yet.</p>`);
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
    const [summary, usersResp, breakdown] = await Promise.all([
      adminApi("/api/admin/users/summary"),
      adminApi("/api/admin/users"),
      adminApi("/api/admin/stats/solved-by-category"),
    ]);
    cards.innerHTML = renderSummary(summary);
    document.getElementById("categoryChart").innerHTML =
      `<div class="chart-title">Solved problems, platform-wide</div>${renderCategoryPie(breakdown, 140)}`;
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
