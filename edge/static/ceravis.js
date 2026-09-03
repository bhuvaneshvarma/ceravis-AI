

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  const isJson = (r.headers.get("Content-Type") || "").includes("json");
  const body = isJson ? await r.json() : await r.blob();
  if (!r.ok) throw body;
  return body;
}

function toast(msg, kind = "ok", ms = 2600) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/* Top-right save/status popup — green on success, red on failure. Used for the
   saveCamera / sync result on setup Step 2 (Continue) and cameras.html (Save). */
function cvNotify(msg, ok = true, ms = 3000) {
  document.querySelectorAll(".cv-notify").forEach(t => t.remove());
  const n = document.createElement("div");
  n.className = "cv-notify " + (ok ? "ok" : "err");
  n.innerHTML = `<span class="ic">${ok ? "&#10003;" : "&#10007;"}</span><span></span>`;
  n.lastChild.textContent = msg;
  document.body.appendChild(n);
  setTimeout(() => n.remove(), ms);
}

/* Display a label without underscores — enum-style ids (LIVING_ROOM, CAM_01)
   never show an underscore in the UI. Value sent to the API is untouched. */
function prettyLabel(s) {
  return String(s == null ? "" : s).replace(/_/g, " ");
}

/* Human-readable form of an enum-ish value for display: underscores → spaces,
   Title Case (CARE_RECIPIENT → "Care Recipient", TIER_1 → "Tier 1", MALE →
   "Male"). Use for role/tier/gender-style fields — NOT for ids/tokens/emails. */
function prettyValue(s) {
  s = String(s == null ? "" : s).trim();
  if (!s) return "";
  return s.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

/* Premium in-app confirm dialog (replaces the browser confirm()). Returns a
   Promise<boolean>. `message` is plain text; opts: {title, confirmText,
   cancelText, danger}. Esc / click-outside = cancel, Enter = confirm. */
function cvConfirm(message, opts = {}) {
  return new Promise(resolve => {
    document.querySelectorAll(".cv-dialog-scrim").forEach(d => d.remove());
    const scrim = document.createElement("div");
    scrim.className = "cv-dialog-scrim";
    scrim.innerHTML = `
      <div class="cv-dialog" role="dialog" aria-modal="true">
        <div class="cv-dialog-title"></div>
        <div class="cv-dialog-msg"></div>
        <div class="cv-dialog-actions">
          <button class="btn btn-ghost" data-cv="cancel"></button>
          <button class="btn ${opts.danger ? "btn-danger" : "btn-primary"}" data-cv="ok"></button>
        </div>
      </div>`;
    scrim.querySelector(".cv-dialog-title").textContent = opts.title || "Please confirm";
    scrim.querySelector(".cv-dialog-msg").textContent = message;
    scrim.querySelector('[data-cv="cancel"]').textContent = opts.cancelText || "Cancel";
    scrim.querySelector('[data-cv="ok"]').textContent = opts.confirmText || "Confirm";
    document.body.appendChild(scrim);
    const done = (v) => { scrim.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    scrim.querySelector('[data-cv="ok"]').onclick = () => done(true);
    scrim.querySelector('[data-cv="cancel"]').onclick = () => done(false);
    scrim.addEventListener("click", e => { if (e.target === scrim) done(false); });
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done(false); }
      // Enter confirms only NON-destructive dialogs; a danger action must be
      // clicked deliberately so a stray Enter can't delete/sign-out.
      else if (e.key === "Enter" && !opts.danger) { e.preventDefault(); done(true); }
    };
    document.addEventListener("keydown", onKey);
    scrim.querySelector(opts.danger ? '[data-cv="cancel"]' : '[data-cv="ok"]').focus();
  });
}

const CV_SESSION_KEY = "cv-session";
const CV_IDLE_MS = 15 * 60 * 1000;

function cvSessionValid() {
  try {
    const s = JSON.parse(localStorage.getItem(CV_SESSION_KEY) || "null");

    return !!(s && s.ts);
  } catch (_) { return false; }
}
function cvStartSession(user) {
  try {
    localStorage.setItem(CV_SESSION_KEY,
      JSON.stringify({ ts: Date.now(), email: (user && user.email) || "" }));
  } catch (_) {}
  cvArmIdle();
}
function cvBumpSession() {
  if (!cvSessionValid()) return;
  try {
    const s = JSON.parse(localStorage.getItem(CV_SESSION_KEY) || "{}");
    s.ts = Date.now();
    localStorage.setItem(CV_SESSION_KEY, JSON.stringify(s));
  } catch (_) {}
}
function cvSignOut() {
  try { localStorage.removeItem(CV_SESSION_KEY); } catch (_) {}
  location.replace("setup.html");
}

function cvRequireAuth() {
  if (cvSessionValid()) { cvArmIdle(); return true; }
  location.replace("setup.html");
  return false;
}
let _cvIdleArmed = false;
function cvArmIdle() {

  if (_cvIdleArmed) return;
  _cvIdleArmed = true;
}

const CERAVIS_LOGO = `<img class="logo-img" src="ceravis-logo.png" alt="Ceravis Health" />`;

function ensureFavicon() {
  if (document.querySelector('link[rel="icon"]')) return;
  const link = document.createElement("link");
  link.rel = "icon"; link.type = "image/png"; link.href = "favicon.png";
  document.head.appendChild(link);
}

const NAV_ICONS = {
  setup:   `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>`,
  live:    `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`,
  cam:     `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>`,
  zones:   `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 20 3 17V4l6 3 6-3 6 3v13l-6-3-6 3Z"/><path d="M9 7v13M15 4v13"/></svg>`,
  hotspot: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5a10 10 0 0 1 14 0M8.5 15.8a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/></svg>`,
  signout: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>`,
};

function renderNav(active) {
  ensureFavicon();
  const items = [
    ["setup.html",     "Setup",      NAV_ICONS.setup],
    ["live.html",      "Live Wall",  NAV_ICONS.live],
    ["cameras.html",   "Cameras",    NAV_ICONS.cam],
    ["zones.html",     "Zones",      NAV_ICONS.zones],
    ["hotspot.html",   "Hotspot",    NAV_ICONS.hotspot],
  ];
  const nav = items.map(([href, label, icon]) =>
    `<a href="${href}" class="side-item ${active === href ? "active" : ""}" title="${label}">${icon}<span>${label}</span></a>`
  ).join("");
  const aside = document.createElement("aside");
  aside.className = "sidebar";
  aside.innerHTML = `
    <div class="side-top">
      <a class="side-brand" href="live.html"><span class="logo-chip">${CERAVIS_LOGO}</span></a>
      <button class="side-collapse" id="cv-collapse" aria-label="Collapse sidebar" title="Collapse">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>
      </button>
    </div>
    <nav class="side-nav">${nav}
      <button type="button" class="side-item side-signout" id="cv-signout" title="Sign out">
        ${NAV_ICONS.signout}<span>Sign out</span>
      </button>
    </nav>
    <div class="side-foot">&copy; 2026 Ceravis Health<span>All Rights Reserved</span></div>`;

  const header = document.createElement("header");
  header.className = "app-header";
  header.innerHTML = `
    <button class="side-toggle" id="cv-side-toggle" aria-label="Menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
    </button>
    <div class="app-eyebrow">Edge Console</div>
    <div class="spacer"></div>
    <span class="dev-pill"><span class="dev-dot"></span>Edge device</span>
    <span class="clock" id="cv-clock"></span>`;

  document.body.prepend(header);
  document.body.prepend(aside);
  document.body.classList.add("has-sidebar");
  cvArmIdle();

  const activeIcon = (items.find(i => i[0] === active) || [])[2] || NAV_ICONS.setup;
  const ph = document.querySelector(".page-head");
  if (ph && !ph.dataset.enhanced) {
    ph.dataset.enhanced = "1";
    const text = document.createElement("div");
    while (ph.firstChild) text.appendChild(ph.firstChild);
    const badge = document.createElement("div");
    badge.className = "ph-icon";
    badge.innerHTML = activeIcon;
    ph.classList.add("ph-flex");
    ph.append(badge, text);
  }

  const signout = document.getElementById("cv-signout");
  if (signout) signout.onclick = async () => {
    if (await cvConfirm("You'll need to sign in again to access this console.",
      { title: "Sign out?", confirmText: "Sign out", danger: true })) cvSignOut();
  };

  // Mobile off-canvas sidebar: a scrim backdrop makes it reliable — tapping
  // outside, choosing an item, or Esc all close it (previously only the toggle
  // did, so it could get stuck open on a phone).
  let scrim = document.querySelector(".sidebar-scrim");
  if (!scrim) { scrim = document.createElement("div"); scrim.className = "sidebar-scrim"; document.body.appendChild(scrim); }
  const setSide = (open) => {
    aside.classList.toggle("open", open);
    scrim.classList.toggle("show", open);
  };
  const toggle = document.getElementById("cv-side-toggle");
  if (toggle) toggle.onclick = () => setSide(!aside.classList.contains("open"));
  scrim.onclick = () => setSide(false);
  aside.querySelectorAll(".side-item").forEach(a =>
    a.addEventListener("click", () => setSide(false)));
  // One global Esc handler (bound once) closes whichever sidebar is open — avoids
  // stacking a new listener each time renderNav runs.
  if (!window.__cvSideEscBound) {
    window.__cvSideEscBound = true;
    window.addEventListener("keydown", e => {
      if (e.key !== "Escape") return;
      document.querySelector(".sidebar.open")?.classList.remove("open");
      document.querySelector(".sidebar-scrim.show")?.classList.remove("show");
    });
  }

  const COLLAPSE_KEY = "cv-sidebar-collapsed";
  let collapsed = false;
  try { collapsed = localStorage.getItem(COLLAPSE_KEY) === "1"; } catch (_) {}
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const collapseBtn = document.getElementById("cv-collapse");
  if (collapseBtn) collapseBtn.onclick = () => {
    const on = !document.body.classList.contains("sidebar-collapsed");
    document.body.classList.toggle("sidebar-collapsed", on);
    try { localStorage.setItem(COLLAPSE_KEY, on ? "1" : "0"); } catch (_) {}
  };

  const tick = () => {
    const el = document.getElementById("cv-clock");
    if (el) el.textContent = cvDateTime();
  };
  tick(); setInterval(tick, 1000);
}

/* Header date-time: dd Mon yyyy · HH:MM:SS (month in text, 24-hour). */
const CV_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function cvDateTime(d = new Date()) {
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getDate())} ${CV_MONTHS[d.getMonth()]} ${d.getFullYear()}`
       + ` · ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function fmtTime(d = new Date()) {
  return d.toLocaleTimeString([], { hour12: false });
}
function healthPill(state) {
  const map = {
    running: ["pill-ok", "LIVE"], connecting: ["pill-warn", "CONNECTING"],
    reconnecting: ["pill-warn", "RECONNECTING"], offline: ["pill-bad", "OFFLINE"],
  };
  const [cls, label] = map[(state || "").toLowerCase()] || ["pill-mute", state || "—"];
  return `<span class="pill ${cls}">${label}</span>`;
}
