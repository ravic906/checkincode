/*
 * Clerk auth wiring. Loaded before app.js so `getAuthToken()` and
 * `isSignedIn()` are available to it. The Clerk script tag in index.html
 * sets up `window.Clerk` asynchronously -- everything here waits on
 * `Clerk.load()` before touching it.
 *
 * This is additive, not a hard cutover yet: app.js still generates/uses
 * its own anonymous X-User-Id for API calls. Once a user signs in, we
 * also attach their Clerk session token as an Authorization header
 * (see api() in app.js) -- the backend doesn't verify it yet (that's the
 * next piece), so right now this only gets the frontend sign-in/sign-out
 * experience working end-to-end before wiring the backend to trust it.
 */

let clerkReady = null; // Promise, resolves once Clerk.load() has finished

function waitForClerk() {
  if (clerkReady) return clerkReady;
  clerkReady = new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      attempts++;
      if (window.Clerk) {
        console.log("[auth] window.Clerk found after", attempts * 50, "ms, calling load()...");
        window.Clerk.load()
          .then(() => { console.log("[auth] Clerk.load() resolved"); resolve(window.Clerk); })
          .catch((e) => { console.error("[auth] Clerk.load() REJECTED:", e); reject(e); });
      } else if (attempts > 200) { // 10s
        console.error("[auth] window.Clerk never appeared after 10s -- script tag likely failed to load/execute");
        reject(new Error("Clerk script never loaded"));
      } else {
        setTimeout(check, 50);
      }
    };
    check();
  });
  return clerkReady;
}

async function getAuthToken() {
  const Clerk = await waitForClerk();
  if (!Clerk.session) return null;
  try {
    return await Clerk.session.getToken();
  } catch {
    return null;
  }
}

function isSignedIn() {
  return !!(window.Clerk && window.Clerk.user);
}

function currentUserEmail() {
  if (!window.Clerk || !window.Clerk.user) return null;
  return window.Clerk.user.primaryEmailAddress?.emailAddress || null;
}

function renderAuthSection() {
  const el = document.getElementById("authSection");
  if (!el || !window.Clerk) return;

  if (window.Clerk.user) {
    el.innerHTML = "";
    window.Clerk.mountUserButton(el);
  } else {
    el.innerHTML = `<button class="signin-btn" id="signInBtn">Sign In</button>`;
    document.getElementById("signInBtn").onclick = () => window.Clerk.openSignIn({});
  }
}

waitForClerk().then((Clerk) => {
  renderAuthSection();
  Clerk.addListener(() => renderAuthSection());
}).catch((e) => {
  console.error("[auth] Clerk setup failed, sign-in unavailable:", e);
});
