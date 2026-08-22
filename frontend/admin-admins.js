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
  const label = a.email || a.id;
  const since = a.created_at ? new Date(a.created_at).toLocaleDateString() : "—";
  return `
    <tr>
      <td>${escapeHtml(label)}</td>
      <td>${since}</td>
    </tr>
  `;
}

async function loadAdmins() {
  const body = document.getElementById("adminsBody");
  body.innerHTML = `<tr><td colspan="2"><div class="loading-dots">Loading…</div></td></tr>`;
  try {
    const data = await adminApi("/api/admin/admins");
    body.innerHTML = data.admins.length
      ? data.admins.map(renderAdminRow).join("")
      : `<tr><td colspan="2" class="empty-note">No admins found.</td></tr>`;
  } catch (err) {
    body.innerHTML = `<tr><td colspan="2"><div class="error-banner">${escapeHtml(err.message)}</div></td></tr>`;
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
  const userId = document.getElementById("setAdminUserId").value.trim();
  if (!userId) return alert("Enter a user_id first (or click \"Use my account\").");
  const verb = isAdminValue ? "Grant" : "Revoke";
  if (!confirm(`${verb} admin rights for:\n${userId}?`)) return;
  try {
    await adminApi("/api/admin/set-admin", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, is_admin: isAdminValue }),
    });
    alert(`Done -- ${userId} admin status is now ${isAdminValue}.`);
    await loadAdmins();
  } catch (err) {
    alert(`${verb} admin failed: ${err.message}`);
  }
}

document.getElementById("fillMyUserIdBtn").onclick = fillMyUserId;
document.getElementById("grantAdminBtn").onclick = () => setAdmin(true);
document.getElementById("revokeAdminBtn").onclick = () => setAdmin(false);

loadAdmins();
