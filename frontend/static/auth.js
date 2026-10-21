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
        <label class="terms-check" id="terms-check" hidden>
          <input type="checkbox" id="terms-agree">
          <span>I agree to the <a href="/terms" target="_blank" rel="noopener">Terms of Service</a>
            and <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a></span>
        </label>
        <button type="submit" id="auth-submit">Sign in</button>
      </form>
      <div class="auth-divider">or</div>
      <button type="button" class="google-btn" id="google-btn">
        <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>
        Continue with Google
      </button>
      <p class="auth-consent" id="auth-consent">By continuing, you agree to our
        <a href="/terms" target="_blank" rel="noopener">Terms of Service</a> and
        <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a>.</p>
      <div class="auth-message" id="auth-message" role="alert" hidden></div>
    </div>
  </dialog>
`);

document.body.insertAdjacentHTML("beforeend", `
  <dialog class="auth upgrade" id="upgrade-dialog" aria-labelledby="upgrade-title">
    <div class="auth-body">
      <div class="auth-head">
        <h2 id="upgrade-title"><span class="pro-badge big">PRO</span> Upgrade to Pro</h2>
        <button type="button" class="auth-close" id="upgrade-close" aria-label="Close">×</button>
      </div>
      <p class="auth-reason" id="upgrade-reason"></p>
      <ul class="upgrade-list">
        <li>Unlimited AI questions</li>
        <li>Save unlimited schools and compare up to 10</li>
        <li>Deadline calendar, CSV export and application checklists</li>
        <li>"Why this school?" fit analysis and all student reviews</li>
      </ul>
      <a class="upgrade-cta" href="/pricing">See Pro for $9/month →</a>
      <button type="button" class="link-btn upgrade-later" id="upgrade-later">Not now</button>
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
  if (!res.ok) {
    const detail = typeof data.detail === "object" && data.detail ? data.detail : { message: data.detail };
    const err = new Error(detail.message || "Something went wrong");
    err.upgrade = Boolean(detail.upgrade);
    throw err;
  }
  return data;
}

// ---------- Plans (Free / Pro) ----------

let plan = { plan: "free", limits: { daily_questions: 10, saved_schools: 3, compare_schools: 3 } };

function isPro() {
  return plan.plan === "pro";
}

async function loadPlan() {
  try {
    const res = await fetch("/api/subscription", {
      headers: session ? { Authorization: `Bearer ${session.access_token}` } : {},
    });
    if (res.ok) plan = await res.json();
  } catch { /* keep the free defaults */ }
  updatePlanUI();
  window.onPlanChanged?.(plan);
  return plan;
}

function showUpgrade(message) {
  document.getElementById("upgrade-reason").textContent =
    message || "Get unlimited questions and every tool for building your college list.";
  const dialog = document.getElementById("upgrade-dialog");
  if (!dialog.open) dialog.showModal();
}

// Error from api(): show the upgrade prompt for Pro-only actions, otherwise a toast.
function handleError(err) {
  if (err.message === "signed out") return;
  if (err.upgrade) showUpgrade(err.message);
  else showToast(err.message);
}

async function openBillingPortal() {
  try {
    const { url } = await api("/api/billing-portal", { method: "POST" });
    location.href = url;
  } catch (err) { handleError(err); }
}

// Download a Pro file (.ics, .csv) with the sign-in token, which a plain link can't send.
async function downloadFile(path, filename) {
  if (!session) { openAuth("signin", "Sign in to download."); return; }
  const res = await fetch(path, { headers: { Authorization: `Bearer ${session.access_token}` } });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = typeof data.detail === "object" && data.detail ? data.detail : { message: data.detail };
    handleError(Object.assign(new Error(detail.message || "Download failed"), { upgrade: Boolean(detail.upgrade) }));
    return;
  }
  const url = URL.createObjectURL(await res.blob());
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function updatePlanUI() {
  document.body.classList.toggle("is-pro", isPro());
  // "Upgrade" button in the header (not on the landing or pricing pages).
  const account = document.querySelector("header .header-inner > .account");
  const skip = ["/", "/pricing"].includes(location.pathname);
  if (account && !skip && !document.getElementById("upgrade-btn")) {
    account.insertAdjacentHTML("afterbegin",
      `<a class="header-btn upgrade-btn" id="upgrade-btn" href="/pricing">⚡<span class="btn-label"> Upgrade</span></a>`);
  }
  const upgrade = document.getElementById("upgrade-btn");
  if (upgrade) upgrade.hidden = isPro();
  const avatar = document.getElementById("avatar-btn");
  if (avatar) avatar.classList.toggle("pro", isPro());
  const badge = document.getElementById("menu-plan");
  if (badge) badge.innerHTML = isPro() ? `<span class="pro-badge">PRO</span> Pro plan` : "Free plan";
  const billing = document.getElementById("menu-billing");
  if (billing) billing.textContent = isPro() ? "Manage billing" : "Upgrade to Pro";
}

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  document.getElementById("tab-signin").setAttribute("aria-selected", String(!signup));
  document.getElementById("tab-signup").setAttribute("aria-selected", String(signup));
  document.getElementById("auth-submit").textContent = signup ? "Create account" : "Sign in";
  document.getElementById("auth-password").autocomplete = signup ? "new-password" : "current-password";
  // New accounts tick the box; the sign-in tab notes that continuing with Google means agreeing.
  document.getElementById("terms-check").hidden = !signup;
  document.getElementById("auth-consent").hidden = signup;
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

// Account button in the header: the user's Google photo (or their initial) opens a small menu.
function ensureAccountMenu(box) {
  if (document.getElementById("avatar-btn")) return;
  box.insertAdjacentHTML("beforeend", `
    <div class="user-menu-wrap">
      <button type="button" class="avatar-btn" id="avatar-btn" aria-haspopup="menu" aria-expanded="false"
              aria-controls="user-menu" aria-label="Account menu"></button>
      <div class="user-menu" id="user-menu" role="menu" hidden>
        <div class="user-menu-name" id="user-menu-name"></div>
        <div class="user-menu-email" id="user-menu-email"></div>
        <div class="user-menu-plan" id="menu-plan">Free plan</div>
        <button type="button" role="menuitem" id="menu-billing">Upgrade to Pro</button>
        <button type="button" role="menuitem" id="menu-sign-out">Sign out</button>
      </div>
    </div>`);
  const button = document.getElementById("avatar-btn");
  const menu = document.getElementById("user-menu");
  const setOpen = open => { menu.hidden = !open; button.setAttribute("aria-expanded", String(open)); };
  button.addEventListener("click", () => setOpen(menu.hidden));
  document.addEventListener("click", event => { if (!event.target.closest(".user-menu-wrap")) setOpen(false); });
  document.addEventListener("keydown", event => { if (event.key === "Escape") setOpen(false); });
  document.getElementById("menu-sign-out").addEventListener("click", () => { setOpen(false); sb?.auth.signOut(); });
  document.getElementById("menu-billing").addEventListener("click", () => {
    setOpen(false);
    if (isPro()) openBillingPortal();
    else location.href = "/pricing";
  });
  updatePlanUI();
}

function profileOf(user) {
  const meta = user?.user_metadata || {};
  const picture = meta.avatar_url || meta.picture || "";
  return {
    name: meta.full_name || meta.name || "",
    email: user?.email || "",
    picture: /^https:\/\//.test(picture) ? picture : "",
  };
}

function updateAuthUI() {
  const signedIn = Boolean(session);
  document.body.classList.toggle("signed-in", signedIn);
  const signedOut = document.getElementById("signed-out");
  const signedInBox = document.getElementById("signed-in");
  if (signedOut) signedOut.hidden = !authReady || signedIn;
  if (!signedInBox) return;
  signedInBox.hidden = !signedIn;
  if (!signedIn) return;
  ensureAccountMenu(signedInBox);
  const user = profileOf(session.user);
  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase();
  const avatar = document.getElementById("avatar-btn");
  avatar.innerHTML = user.picture
    ? `<img src="${escapeHtml(user.picture)}" alt="" referrerpolicy="no-referrer">`
    : `<span>${escapeHtml(initial)}</span>`;
  avatar.querySelector("img")?.addEventListener("error", () => {
    avatar.innerHTML = `<span>${escapeHtml(initial)}</span>`;  // photo failed to load
  });
  avatar.classList.toggle("pro", isPro());
  document.getElementById("user-menu-name").textContent = user.name;
  document.getElementById("user-menu-email").textContent = user.email;
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
    handleError(err);
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
      if (!termsAccepted()) return;
      const { data, error } = await sb.auth.signUp({
        email,
        password,
        options: {
          emailRedirectTo: location.origin + location.pathname,
          data: { terms_accepted_at: new Date().toISOString(), terms_version: TERMS_VERSION },
        },
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

const TERMS_VERSION = "2026-10";

function termsAccepted() {
  if (document.getElementById("terms-agree").checked) return true;
  setAuthMessage("Please agree to the Terms of Service and Privacy Policy to create an account.");
  document.getElementById("terms-agree").focus();
  return false;
}

async function signInWithGoogle() {
  if (authMode === "signup" && !termsAccepted()) return;
  await authSettings;  // whether Google is turned on in Supabase
  if (!googleEnabled) {
    setAuthMessage("Google sign-in isn't turned on yet. Please use email for now.");
    return;
  }
  const button = document.getElementById("google-btn");
  button.disabled = true;
  const { error } = await sb.auth.signInWithOAuth({
    provider: "google", options: { redirectTo: `${location.origin}/` },  // homepage; it forwards signed-in users to /chat
  });
  if (error) {
    button.disabled = false;
    setAuthMessage(error.message);
  }
}

// A failed or cancelled Google sign-in comes back with ?error_description=... in the URL.
function takeAuthErrorFromUrl() {
  const query = new URLSearchParams(location.search);
  const hash = new URLSearchParams(location.hash.slice(1));
  const message = query.get("error_description") || hash.get("error_description");
  if (!message) return null;
  for (const key of ["error", "error_code", "error_description"]) query.delete(key);
  const rest = query.toString();
  history.replaceState(history.state, "", location.pathname + (rest ? `?${rest}` : ""));
  return message;
}

let authSettings = Promise.resolve();

async function initAuth() {
  const authError = takeAuthErrorFromUrl();
  if (authError) setTimeout(() => showToast(`Sign-in didn't finish: ${authError}`), 300);
  try {
    const config = (await (await fetch("/config")).json()).supabase || {};
    if (!config.url || !config.anon_key || !window.supabase) { await loadPlan(); return; }
    sb = window.supabase.createClient(config.url, config.anon_key, {
      auth: { flowType: "pkce", persistSession: true, detectSessionInUrl: true },
    });
    authReady = true;
    authSettings = fetch(`${config.url}/auth/v1/settings`, { headers: { apikey: config.anon_key } })
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
        loadPlan().finally(() => window.onAuthChanged?.(event));
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

document.getElementById("saved-btn")?.addEventListener("click", () => {
  if (location.pathname !== "/chat") location.href = "/chat#saved";
  else if (location.hash === "#saved") window.onSavedClick?.();
  else location.hash = "saved";
});

document.getElementById("upgrade-close").addEventListener("click", () => document.getElementById("upgrade-dialog").close());
document.getElementById("upgrade-later").addEventListener("click", () => document.getElementById("upgrade-dialog").close());
document.getElementById("upgrade-dialog").addEventListener("click", event => {
  if (event.target.id === "upgrade-dialog") event.target.close();
});
