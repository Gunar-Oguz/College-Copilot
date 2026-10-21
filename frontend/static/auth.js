/*
 * Sign-in and saved schools, shared by every page (chat, school profiles, compare).
 * Supabase Auth runs in the browser; the server verifies the access token on /me/* calls.
 * Pages can define window.onAuthChanged(event) to react to sign-in and sign-out.
 */

document.body.insertAdjacentHTML("beforeend", `
  <dialog class="auth" id="auth-dialog" aria-labelledby="auth-title">
    <div class="auth-body">
      <div class="auth-head">
        <h2 id="auth-title">Welcome to CollegeCopilot</h2>
        <button type="button" class="auth-close" id="auth-close" aria-label="Close">×</button>
      </div>
      <p class="auth-reason" id="auth-reason">Sign in to save schools and keep notes on them.</p>
      <div class="auth-tabs" role="tablist">
        <button type="button" role="tab" id="tab-signin" aria-selected="true">Sign in</button>
        <button type="button" role="tab" id="tab-signup" aria-selected="false">Create account</button>
      </div>
      <form id="auth-form" novalidate>
        <label>Email <input type="email" id="auth-email" autocomplete="email" required></label>
        <label>Password <input type="password" id="auth-password" autocomplete="current-password" minlength="6" required></label>
        <button type="submit" id="auth-submit">Sign in</button>
      </form>
      <div class="auth-divider">or</div>
      <button type="button" class="google-btn" id="google-btn">
        <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>
        Continue with Google
      </button>
      <div class="auth-message" id="auth-message" role="alert" hidden></div>
    </div>
  </dialog>
`);

const authDialog = document.getElementById("auth-dialog");
const authForm = document.getElementById("auth-form");
const authMessage = document.getElementById("auth-message");

let sb = null;            // Supabase client, once /config says sign-in is set up
let session = null;       // current Supabase session
let authReady = false;    // sign-in is available on this server
let googleEnabled = false;
let authMode = "signin";
const favorites = new Set();

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Heart button for a school; its state comes from the signed-in user's favorites.
function saveButton(schoolId, name) {
  if (!schoolId || !authReady) return "";
  const saved = favorites.has(schoolId);
  return `<button type="button" class="save-btn" data-school-id="${schoolId}"
    aria-pressed="${saved}" aria-label="Save ${escapeHtml(name)}" title="Save school">${saved ? "♥" : "♡"}</button>`;
}

// ---------- Accounts (Supabase Auth) ----------

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${session?.access_token || ""}`,
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    openAuth("signin", "Your session expired. Please sign in again.");
    throw new Error("signed out");
  }
  if (!res.ok) throw new Error(data.detail || "Something went wrong");
  return data;
}

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  document.getElementById("tab-signin").setAttribute("aria-selected", String(!signup));
  document.getElementById("tab-signup").setAttribute("aria-selected", String(signup));
  document.getElementById("auth-submit").textContent = signup ? "Create account" : "Sign in";
  document.getElementById("auth-password").autocomplete = signup ? "new-password" : "current-password";
  setAuthMessage("");
}

function setAuthMessage(text, kind = "error") {
  authMessage.hidden = !text;
  authMessage.textContent = text;
  authMessage.className = `auth-message ${kind}`;
}

function openAuth(mode = "signin", reason = "Sign in to save schools and keep notes on them.") {
  if (!authReady) return;
  setAuthMode(mode);
  document.getElementById("auth-reason").textContent = reason;
  if (!authDialog.open) authDialog.showModal();
  document.getElementById("auth-email").focus();
}

function updateAuthUI() {
  const signedIn = Boolean(session);
  document.body.classList.toggle("signed-in", signedIn);
  const signedOut = document.getElementById("signed-out");
  const signedInBox = document.getElementById("signed-in");
  const email = document.getElementById("user-email");
  if (signedOut) signedOut.hidden = !authReady || signedIn;
  if (signedInBox) signedInBox.hidden = !signedIn;
  if (email) email.textContent = session?.user?.email || "";
}

function refreshHearts() {
  document.querySelectorAll(".save-btn").forEach(btn => {
    const saved = favorites.has(Number(btn.dataset.schoolId));
    btn.setAttribute("aria-pressed", String(saved));
    btn.textContent = saved ? "♥" : "♡";
  });
  const count = document.getElementById("saved-count");
  if (count) count.textContent = favorites.size;
}

async function loadFavoriteIds() {
  try {
    const data = await api("/me/favorite-ids");
    favorites.clear();
    data.ids.forEach(id => favorites.add(id));
  } catch { /* api() already asked the user to sign in again */ }
  refreshHearts();
}

async function toggleSave(schoolId) {
  if (!session) {
    openAuth("signin", "Sign in to save schools.");
    return;
  }
  const wasSaved = favorites.has(schoolId);
  wasSaved ? favorites.delete(schoolId) : favorites.add(schoolId);
  refreshHearts();
  try {
    await api(`/me/favorites/${schoolId}`, { method: wasSaved ? "DELETE" : "PUT" });
    showToast(wasSaved ? "Removed from saved schools" : "Saved to your schools");
  } catch (err) {
    wasSaved ? favorites.add(schoolId) : favorites.delete(schoolId);
    refreshHearts();
    if (err.message !== "signed out") showToast(err.message);
  }
}

async function submitAuth(event) {
  event.preventDefault();
  const email = document.getElementById("auth-email").value.trim();
  const password = document.getElementById("auth-password").value;
  if (!email || password.length < 6) {
    setAuthMessage("Enter your email and a password of at least 6 characters.");
    return;
  }
  const submit = document.getElementById("auth-submit");
  submit.disabled = true;
  try {
    if (authMode === "signup") {
      const { data, error } = await sb.auth.signUp({
        email, password, options: { emailRedirectTo: location.origin + location.pathname },
      });
      if (error) throw error;
      if (!data.session) {
        setAuthMessage(`Check ${email} for a link to confirm your account, then sign in.`, "info");
      }
    } else {
      const { error } = await sb.auth.signInWithPassword({ email, password });
      if (error) throw error;
    }
  } catch (err) {
    setAuthMessage(err.message || "Could not sign in.");
  } finally {
    submit.disabled = false;
  }
}

async function signInWithGoogle() {
  if (!googleEnabled) {
    setAuthMessage("Google sign-in isn't turned on yet. Please use email for now.");
    return;
  }
  const { error } = await sb.auth.signInWithOAuth({
    provider: "google", options: { redirectTo: location.origin + location.pathname },
  });
  if (error) setAuthMessage(error.message);
}

async function initAuth() {
  try {
    const config = (await (await fetch("/config")).json()).supabase || {};
    if (!config.url || !config.anon_key || !window.supabase) return;
    sb = window.supabase.createClient(config.url, config.anon_key, {
      auth: { flowType: "pkce", persistSession: true, detectSessionInUrl: true },
    });
    authReady = true;
    fetch(`${config.url}/auth/v1/settings`, { headers: { apikey: config.anon_key } })
      .then(r => r.json()).then(s => { googleEnabled = Boolean(s.external?.google); }).catch(() => {});
    sb.auth.onAuthStateChange((event, newSession) => {
      session = newSession;
      updateAuthUI();
      // Supabase advises not to await other calls inside this callback.
      setTimeout(() => {
        if (session) {
          loadFavoriteIds();
          if (event === "SIGNED_IN" && authDialog.open) authDialog.close();
        } else {
          favorites.clear();
          refreshHearts();
        }
        window.onAuthChanged?.(event);
      }, 0);
    });
  } catch {
    authReady = false;
  }
  updateAuthUI();
}

const fmt = {
  pct: v => (v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`),
  money: v => (v == null ? "—" : `$${Math.round(Number(v)).toLocaleString()}`),
  int: v => (v == null ? "—" : Math.round(Number(v)).toLocaleString()),
  date: v => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }),
};

document.addEventListener("click", event => {
  const trigger = event.target.closest("[data-auth]");
  if (trigger) openAuth(trigger.dataset.auth === "signup" ? "signup" : "signin",
    trigger.closest(".sign-in-hint") ? "Sign in to save schools." : undefined);
});
document.getElementById("tab-signin").addEventListener("click", () => setAuthMode("signin"));
document.getElementById("tab-signup").addEventListener("click", () => setAuthMode("signup"));
document.getElementById("auth-close").addEventListener("click", () => authDialog.close());
authDialog.addEventListener("click", event => { if (event.target === authDialog) authDialog.close(); });
authForm.addEventListener("submit", submitAuth);
document.getElementById("google-btn").addEventListener("click", signInWithGoogle);
document.getElementById("sign-out")?.addEventListener("click", () => sb?.auth.signOut());

document.getElementById("saved-btn")?.addEventListener("click", () => {
  if (location.pathname !== "/") location.href = "/#saved";
  else if (location.hash === "#saved") window.onSavedClick?.();
  else location.hash = "saved";
});
