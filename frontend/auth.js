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

// Clerk fires its listener on lots of internal state changes (token
// refresh, focus, etc.), not just sign-in/out. mountUserButton() renders a
// React tree into #authSection -- wiping it with innerHTML="" and
// remounting on every single listener tick (as this used to do) bypasses
// React's own unmount lifecycle, corrupting its internal tree over
// repeated calls until it crashes ("Failed to execute 'removeChild' --
// the node to be removed is not a child of this node") and leaves the
// section empty. Track whether we're currently mounted and only
// mount/unmount on an actual sign-in/out transition.
let _authButtonMounted = false;

function renderAuthSection() {
  const el = document.getElementById("authSection");
  if (!el || !window.Clerk) return;

  if (window.Clerk.user) {
    if (_authButtonMounted) return;
    el.innerHTML = "";
    // Subscription status/upgrade/cancel opens as its own modal (see
    // app.js's openSubscriptionModal), reached via a customMenuItems
    // link right in Clerk's account popover -- a paid account is always
    // a signed-in Clerk account (doUpgrade() in app.js forces sign-in
    // first), so surfacing it from here is the natural single home for
    // it. This was originally a custom page nested inside Clerk's own
    // "Manage account" profile modal (userProfileProps.customPages),
    // which never actually rendered in practice -- this app loads Clerk
    // in a non-standard way (manual ui.ClerkUI injection, see the
    // waitForClerk() comment above) to dodge a Monaco AMD collision, and
    // that path apparently doesn't support nested custom profile pages.
    // customMenuItems is a much simpler contract (just an onClick) with
    // no such gap.
    window.Clerk.mountUserButton(el, {
      customMenuItems: [
        {
          label: "Subscription",
          onClick: () => {
            if (typeof openSubscriptionModal === "function") {
              openSubscriptionModal();
            }
          },
          mountIcon: (iconEl) => { iconEl.textContent = "💳"; },
          unmountIcon: (iconEl) => { if (iconEl) iconEl.textContent = ""; },
        },
      ],
    });
    _authButtonMounted = true;
  } else {
    if (_authButtonMounted) {
      window.Clerk.unmountUserButton(el);
      _authButtonMounted = false;
    }
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
