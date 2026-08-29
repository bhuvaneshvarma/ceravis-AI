/* =====================================================================
   CERAVIS shared front-end helpers: nav, API, toasts
   (live camera video lives in live-view.js — one mechanism, WebRTC only)
   ===================================================================== */

/* The fleet per-edge prefix (for /api on /<edge_id>/ui/… pages) is handled
   globally by fleet-prefix.js, which every page loads BEFORE this file — it
   wraps fetch, so nothing here needs to know the prefix. */

/* ---- tiny API wrapper ---------------------------------------------- */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  const isJson = (r.headers.get("Content-Type") || "").includes("json");
  const body = isJson ? await r.json() : await r.blob();
  if (!r.ok) throw body;
  return body;
}

/* ---- toast notifications ------------------------------------------- */
function toast(msg, kind = "ok", ms = 2600) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/* ---- shared top navigation ------------------------------------------ */
/* The brand mark is the Ceravis Health wordmark — the exact asset the cloud app
   ships (edge/static/ceravis-logo.png), on a white chip so it reads on the teal
   bar the way the app's logo sits on its teal sidebar. */
const CERAVIS_LOGO = `<img class="logo-img" src="ceravis-logo.png" alt="Ceravis Health" />`;

/* Put the Ceravis monogram in the browser tab, once, on every page. */
function ensureFavicon() {
  if (document.querySelector('link[rel="icon"]')) return;
  const link = document.createElement("link");
  link.rel = "icon"; link.type = "image/png"; link.href = "favicon.png";
  document.head.appendChild(link);
}

/* Minimal 20px stroke icons, one per nav destination (Lucide-style). */
const NAV_ICONS = {
  setup:   `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>`,
  live:    `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`,
  monitor: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>`,
  dash:    `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6" rx="1"/><rect x="12" y="7" width="3" height="10" rx="1"/><rect x="17" y="13" width="3" height="4" rx="1"/></svg>`,
  cam:     `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>`,
  zones:   `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 20 3 17V4l6 3 6-3 6 3v13l-6-3-6 3Z"/><path d="M9 7v13M15 4v13"/></svg>`,
  hotspot: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5a10 10 0 0 1 14 0M8.5 15.8a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/></svg>`,
};

/* App-style shell: a fixed teal sidebar (the app's own sidebar colour + active
   pill) plus a slim white top header. Prepended to <body>; content keeps its
   own `.page` container, shifted right by `body.has-sidebar`. */
function renderNav(active) {
  ensureFavicon();
  const items = [
    ["setup.html",     "Setup",      NAV_ICONS.setup],
    ["live.html",      "Live Wall",  NAV_ICONS.live],
    ["monitor.html",   "AI Monitor", NAV_ICONS.monitor],
    ["dashboard.html", "Dashboard",  NAV_ICONS.dash],
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
    <nav class="side-nav">${nav}</nav>
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

  const toggle = document.getElementById("cv-side-toggle");
  if (toggle) toggle.onclick = () => aside.classList.toggle("open");
  aside.querySelectorAll(".side-item").forEach(a =>
    a.addEventListener("click", () => aside.classList.remove("open")));

  // Desktop collapse (icon-only rail), remembered per browser like the app's ← .
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
    if (el) el.textContent = new Date().toLocaleString();
  };
  tick(); setInterval(tick, 1000);
}

/* Live camera video is NOT served by this process — every page plays the same
   MediaMTX WebRTC stream as the public live links, via liveView() in
   live-view.js (loaded by the pages that show cameras). */

/* ---- misc ------------------------------------------------------------ */
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
