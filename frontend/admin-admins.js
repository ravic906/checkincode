/*
 * Not linked from the main nav for non-admins -- reached at
 * /admin-admins.html. Lists current admins (GET /api/admin/admins) and
 * hosts the grant/revoke-admin form (moved here from the Problems page).
 *
 * Deliberately no self-service "claim admin" flow: fillMyUserId() only
 * pre-fills the user_id field as a convenience for whoever is signed in --
 * the actual grant/revoke still goes through POST /api/admin/set-admin,
 * which requires _require_admin() (the admin token, or an already-admin
 * session) to succeed, same gate as every other admin action.
 */

initAdminSidebar("admins");

function renderAdminRow(a) {
  const label = a.full_name || a.email || a.id;
  const showIdSubLabel = label !== a.id;
  const since = a.created_at ? new Date(a.created_at).toLocaleDateString() : "—";
  return `
    <tr>
      <td>
        <div>${escapeHtml(label)}</div>
        ${showIdSubLabel ? `<div class="user-sublabel">${escapeHtml(a.id)}</div>` : ""}
      </td>
      <td>${a.email ? escapeHtml(a.email) : "—"}</td>
      <td>${a.username ? `@${escapeHtml(a.username)}` : "—"}</td>
      <td>${since}</td>
    </tr>
  `;
}

let allAdmins = [];

function renderAdminRows(admins) {
  const body = document.getElementById("adminsBody");
  body.innerHTML = admins.length
    ? admins.map(renderAdminRow).join("")
    : `<tr><td colspan="4" class="empty-note">No admins match.</td></tr>`;
}

function applyAdminSearch() {
  const q = document.getElementById("adminSearchInput").value.trim().toLowerCase();
  if (!q) { renderAdminRows(allAdmins); return; }
  const filtered = allAdmins.filter(a =>
    [a.id, a.email, a.username, a.full_name].some(field => field && field.toLowerCase().includes(q))
  );
  renderAdminRows(filtered);
}

async function loadAdmins() {
  const body = document.getElementById("adminsBody");
  body.innerHTML = `<tr><td colspan="4"><div class="loading-dots">Loading…</div></td></tr>`;
  try {
    const data = await adminApi("/api/admin/admins");
    allAdmins = data.admins;
    renderAdminRows(allAdmins);
  } catch (err) {
    body.innerHTML = `<tr><td colspan="4"><div class="error-banner">${escapeHtml(err.message)}</div></td></tr>`;
  }
}

async function fillMyUserId() {
  try {
    const result = await adminApi("/api/whoami");
    document.getElementById("setAdminUserId").value = result.user_id;
  } catch (err) {
    alert(`Lookup failed: ${err.message}`);
  }
}

async function setAdmin(isAdminValue) {
  const identifier = document.getElementById("setAdminUserId").value.trim();
  if (!identifier) return alert("Enter an email, username, or user_id first (or click \"Use my account\").");
  const verb = isAdminValue ? "Grant" : "Revoke";
  if (!confirm(`${verb} admin rights for:\n${identifier}?`)) return;
  try {
    // The backend resolves identifier (email/username/user_id) to the
    // definitive stored user_id -- shown in the confirmation so it's
    // clear exactly which account changed, not just what was typed.
    const result = await adminApi("/api/admin/set-admin", {
      method: "POST",
      body: JSON.stringify({ user_id: identifier, is_admin: isAdminValue }),
    });
    alert(`Done -- ${result.user_id} admin status is now ${isAdminValue}.`);
    await loadAdmins();
  } catch (err) {
    alert(`${verb} admin failed: ${err.message}`);
  }
}

document.getElementById("fillMyUserIdBtn").onclick = fillMyUserId;
document.getElementById("grantAdminBtn").onclick = () => setAdmin(true);
document.getElementById("revokeAdminBtn").onclick = () => setAdmin(false);
document.getElementById("adminSearchInput").addEventListener("input", applyAdminSearch);

loadAdmins();
