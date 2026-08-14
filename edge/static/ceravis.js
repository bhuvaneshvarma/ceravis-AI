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
const CERAVIS_LOGO = `
<svg class="mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M16 2 L28 7 V15 C28 23 23 28.5 16 30 C9 28.5 4 23 4 15 V7 Z"
        stroke="#2dd4bf" stroke-width="2" fill="rgba(45,212,191,.08)"/>
  <path d="M8 17 H12 L14 12 L17 21 L19 15.5 L20.5 17 H24"
        stroke="#2dd4bf" stroke-width="1.8" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

function renderNav(active) {
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
      <span><span class="word">CERAVIS</span>
      <span class="tag">Care Intelligence &middot; Edge Surveillance</span></span>
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
