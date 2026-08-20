// Set this to your deployed backend's URL (no trailing slash).
// Locally this is left unset so app.js falls back to http://127.0.0.1:8000.
// On Render, set it to the backend web service's URL, e.g.:
//   window.API_BASE = "https://sql-practice-backend-6bpi.onrender.com";
window.API_BASE = "https://sql-practice-backend-6bpi.onrender.com";

// Clerk publishable key -- safe to expose client-side by design (it's
// meant to be public, unlike the secret key which only ever lives in the
// backend's env vars).
window.CLERK_PUBLISHABLE_KEY = "pk_test_ZGFyaW5nLWNhaW1hbi05NDM5LmNsZXJrLmFjY291bnRzLmRldiQ";
