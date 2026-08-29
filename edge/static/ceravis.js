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
/* The brand mark is the Ceravis Health wordmark (edge/static/Logo.png), so the
   edge UI carries the same identity as the cloud app. */
const CERAVIS_LOGO = `<img class="logo-img" src="Logo.png" alt="Ceravis Health" />`;

/* Put the Ceravis monogram in the browser tab, once, on every page. */
function ensureFavicon() {
  if (document.querySelector('link[rel="icon"]')) return;
  const link = document.createElement("link");
  link.rel = "icon"; link.type = "image/png"; link.href = "favicon.png";
  document.head.appendChild(link);
}

function renderNav(active) {
  ensureFavicon();
  const links = [
    ["setup.html",     "Setup"],
    ["live.html",      "Live Wall"],
    ["monitor.html",   "AI Monitor"],
    ["dashboard.html", "Dashboard"],
    ["cameras.html",   "Cameras"],
    ["zones.html",     "Zones"],
    ["hotspot.html",   "Hotspot"],
  ];
  const nav = links.map(([href, label]) =>
    `<a href="${href}" class="${active === href ? "active" : ""}">${label}</a>`
  ).join("");
  const bar = document.createElement("header");
  bar.className = "topbar";
  bar.innerHTML = `
    <a class="brand" href="live.html">
      ${CERAVIS_LOGO}
      <span class="tag">Care Intelligence &middot; Edge Surveillance</span>
    </a>
    <nav class="nav">${nav}</nav>
    <div class="spacer"></div>
    <span class="clock" id="cv-clock"></span>`;
  document.body.prepend(bar);
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
