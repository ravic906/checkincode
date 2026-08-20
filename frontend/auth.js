/*
 * Clerk auth wiring. Loaded before app.js so `getAuthToken()` and
 * `isSignedIn()` are available to it.
 *
 * Both Clerk script tags (@clerk/ui then @clerk/clerk-js, see index.html)
 * are `defer`red, so by the window `load` event they've both finished
 * executing and `window.Clerk` + `window.__internal_ClerkUICtor` exist --
 * this is Clerk's own documented init pattern for the no-bundler script-tag
 * setup, not something to swap back to polling for `window.Clerk` alone
 * (that was the bug: newer clerk-js needs the UI package wired in via
 * Clerk.load({ ui: ... }) before it fully initializes).
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
    window.addEventListener("load", async () => {
      try {
        await window.Clerk.load({
          ui: { ClerkUI: window.__internal_ClerkUICtor },
        });
        resolve(window.Clerk);
      } catch (e) {
        reject(e);
      }
    });
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
