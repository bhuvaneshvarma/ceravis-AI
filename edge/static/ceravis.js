/* =====================================================================
   CERAVIS shared front-end helpers: nav, API, live streams, toasts
   ===================================================================== */

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

/* ---- live JPEG-over-WebSocket stream --------------------------------
   Usage:  const s = liveStream(imgEl, "cam_1", { onFrame: img => ... });
           s.stop();
   Auto-reconnects with backoff; flags stale feeds via onState callback. */
function liveStream(imgEl, cameraId, { onFrame = null, onState = null } = {}) {
  let ws = null, stopped = false, lastUrl = null, retry = 1000, lastMsg = 0;

  const setState = s => onState && onState(s);

  function connect() {
    if (stopped) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/stream/${cameraId}`);
    ws.binaryType = "blob";
    setState("connecting");

    ws.onmessage = (e) => {
      lastMsg = Date.now();
      retry = 1000;
      const url = URL.createObjectURL(e.data);
      imgEl.onload = () => {
        if (lastUrl) URL.revokeObjectURL(lastUrl);
        lastUrl = url;
        setState("live");
        onFrame && onFrame(imgEl);
      };
      imgEl.onerror = () => URL.revokeObjectURL(url);
      imgEl.src = url;
    };
    ws.onclose = () => {
      if (stopped) return;
      setState("offline");
      setTimeout(connect, retry);
      retry = Math.min(retry * 1.7, 10000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }
  connect();

  // stale watchdog: no frame for 6 s -> show as stalled
  const wd = setInterval(() => {
    if (!stopped && lastMsg && Date.now() - lastMsg > 6000) setState("stalled");
  }, 2000);

  return {
    stop() {
      stopped = true;
      clearInterval(wd);
      try { ws && ws.close(); } catch (_) {}
      if (lastUrl) URL.revokeObjectURL(lastUrl);
    },
  };
}

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
