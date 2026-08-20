/*
 * Clerk auth wiring. Loaded before app.js so `getAuthToken()` and
 * `isSignedIn()` are available to it.
 *
 * Clerk's UMD bundle registers itself via an anonymous AMD `define()` when
 * one is present on the page -- and Monaco Editor's loader.js (also on
 * this page, for the SQL editor) installs exactly that kind of global AMD
 * loader. The two collide with "Can only have one anonymous define call
 * per script file" and Clerk's script aborts before ever setting
 * window.Clerk. Fix: load Clerk's scripts dynamically with window.define
 * temporarily hidden, so Clerk falls back to a plain global assignment
 * instead of trying to register as an AMD module; restore window.define
 * afterward so Monaco still works normally.
 *
 * This is additive, not a hard cutover yet: app.js still generates/uses
 * its own anonymous X-User-Id for API calls. Once a user signs in, we
 * also attach their Clerk session token as an Authorization header
 * (see api() in app.js) -- the backend doesn't verify it yet (that's the
 * next piece), so right now this only gets the frontend sign-in/sign-out
 * experience working end-to-end before wiring the backend to trust it.
 */

const CLERK_FRONTEND_API = "daring-caiman-9439.clerk.accounts.dev";
const CLERK_PUBLISHABLE_KEY = window.CLERK_PUBLISHABLE_KEY || "";

function loadScriptNoAmd(src, extraAttrs = {}) {
  return new Promise((resolve, reject) => {
    // Monaco's loader.js declares `define` as a non-configurable global
    // (typical for `var`-declared top-level AMD loaders), so `delete
    // window.define` silently no-ops -- it's still writable though, so
    // reassigning to undefined is what actually hides it.
    const savedDefine = window.define;
    window.define = undefined;

    const s = document.createElement("script");
    s.src = src;
    s.crossOrigin = "anonymous";
    for (const [k, v] of Object.entries(extraAttrs)) s.setAttribute(k, v);
    s.onload = () => { window.define = savedDefine; resolve(); };
    s.onerror = (e) => { window.define = savedDefine; reject(e); };
    document.head.appendChild(s);
  });
}

let clerkReady = null; // Promise, resolves once Clerk.load() has finished

function waitForClerk() {
  if (clerkReady) return clerkReady;
  clerkReady = (async () => {
    await loadScriptNoAmd(`https://${CLERK_FRONTEND_API}/npm/@clerk/ui@1/dist/ui.browser.js`);
    await loadScriptNoAmd(`https://${CLERK_FRONTEND_API}/npm/@clerk/clerk-js@6/dist/clerk.browser.js`, {
      "data-clerk-publishable-key": CLERK_PUBLISHABLE_KEY,
    });
    await window.Clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
    });
    return window.Clerk;
  })();
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
