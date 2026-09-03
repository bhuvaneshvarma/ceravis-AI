/* =====================================================================
   Shared camera-management panel — ONE implementation used by BOTH the
   setup wizard (Step 2) and the dedicated cameras.html page.
   mountCameras(root, { cloudSaveOnAdd }) renders: ONVIF discovery + connect,
   an add-a-camera form (auto-filled RTSP + room), a registered-cameras table
   (start/stop/restart/delete — delete also clears the cloud), and a live
   verification wall (click a tile → fullscreen + PTZ). Returns
   { refresh, syncCameras, destroy }. Depends on ceravis.js (api, toast,
   cvNotify, el, healthPill) and live-view.js (liveView).

   Design notes (kept OUT of the page for a clean, professional UI) live in
   docs/ui-architecture.md.
   ===================================================================== */
function mountCameras(root, opts = {}) {
  const CAM_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>`;
  const ROOMS = ["KITCHEN", "BEDROOM", "LIVING ROOM", "LOUNGE", "LIVE FEED"];

  root.innerHTML = `
  <div class="cam-panel">
    <div class="split">
      <div class="card">
        <h2>Find cameras <span class="faint">(Wi-Fi / ONVIF)</span></h2>
        <div class="hint mb">Scan this network, then sign in with the camera's own username and password.</div>
        <div class="row">
          <button class="btn btn-ghost" id="d-scan">Scan network</button>
          <span class="probe-result" id="d-scan-out"></span>
        </div>
        <div id="d-list" class="mt"></div>
        <div id="d-creds" class="mt" hidden>
          <div class="grid-2">
            <div><label class="f">Camera username</label>
              <input type="text" id="d-user" placeholder="admin" /></div>
            <div><label class="f">Camera password</label>
              <input type="password" id="d-pass" /></div>
          </div>
          <div class="row mt">
            <button class="btn btn-ghost" id="d-probe">Connect &amp; preview</button>
            <span class="probe-result" id="d-probe-out"></span>
          </div>
        </div>
        <hr class="sep" />
        <h2>Add a camera</h2>
        <label class="f">RTSP stream URL <span class="faint">(auto-filled when you connect)</span></label>
        <input type="text" id="c-url" placeholder="rtsp://user:pass@192.168.x.x:554/stream1" />
        <label class="f">Room</label>
        <select id="c-room"><option value="">— select room —</option></select>
        <div class="row mt">
          <button class="btn btn-primary" id="c-save">Save &amp; start</button>
        </div>
        <hr class="sep" />
        <h2>Registered cameras
          <i class="info-i" title="Click a camera in the live wall to open it fullscreen and set its viewing angle.">i</i>
        </h2>
        <table class="tbl"><tbody id="c-table"></tbody></table>
      </div>

      <div class="card">
        <h2>Live verification wall</h2>
        <div class="hint mb">Live view of your saved cameras. Click one to open it fullscreen and set its angle.</div>
        <div class="cam-grid" id="c-grid"></div>
      </div>
    </div>
  </div>`;

  const gid = (id) => document.getElementById(id);
  const streams = {};
  const DISC = { cams: [], sel: null, probed: null, profileToken: null };
  let refreshTimer = null, _edgeId = null, destroyed = false;

  async function edgeId() {
    if (_edgeId !== null) return _edgeId;
    try { const a = await api("/api/v1/account"); _edgeId = (a && a.user && a.user.edgeId) || ""; }
    catch { _edgeId = ""; }
    return _edgeId;
  }

  /* ---- friendly, short messages (never raw server text) ---- */
  function friendlyProbeError(e) {
    const d = ((e && (e.detail || e.message)) || "").toString().toLowerCase();
    if (d.includes("authentic") || d.includes("not authorized") || d.includes("unauthor") || d.includes("401"))
      return "Incorrect camera username or password.";
    if (d.includes("cannot reach") || d.includes("timed out") || d.includes("timeout") || d.includes("refused"))
      return "Camera unreachable — check it's powered on and on this network.";
    return "Couldn't connect — check the login and try again.";
  }
  function friendlyCloudError(reason) {
    const d = (reason || "").toString().toLowerCase();
    if (d.includes("already") || d.includes("duplicate") || d.includes("exists"))
      return "Not saved — this room already has a camera on your account.";
    if (d.includes("verify") || d.includes("account"))
      return "Not saved — verify your account first.";
    return "Not saved — please try again.";
  }

  /* ---- registered cameras (table + controls) + room list + live wall ---- */
  async function refresh() {
    let cams, status;
    try {
      [cams, status] = await Promise.all([
        api("/api/v1/cameras"), api("/api/v1/cameras/status").catch(() => ({})),
      ]);
    } catch { return; }
    if (destroyed) return;
    rebuildRooms(cams);
    const tbody = gid("c-table");
    if (!tbody) return;
    tbody.innerHTML = cams.length === 0
      ? `<tr><td colspan="3"><div class="empty">
           <div class="empty-ic">${CAM_ICON}</div>
           <div class="empty-title">No cameras added yet</div>
           <div class="empty-sub">Scan the network above and connect your first camera.</div>
         </div></td></tr>`
      : cams.map(c => {
          const s = status[c.camera_id] || {};
          return `<tr>
            <td><b>${c.room_name}</b><br><span class="faint">${c.device_label ? c.device_label + " · " : ""}${c.camera_id}</span></td>
            <td>${healthPill(s.health_state)} <span class="faint">${(s.current_fps || 0).toFixed(1)} fps</span></td>
            <td class="cam-actions">
              <button class="btn btn-ghost btn-sm" data-act="start" data-id="${c.camera_id}">Start</button>
              <button class="btn btn-ghost btn-sm" data-act="stop" data-id="${c.camera_id}">Stop</button>
              <button class="btn btn-ghost btn-sm" data-act="restart" data-id="${c.camera_id}">Restart</button>
              <button class="btn btn-danger btn-sm" data-del="${c.camera_id}">Delete</button>
            </td></tr>`;
        }).join("");
    wireTable();
    syncWall(cams);
  }

  /* One camera per room: the dropdown only offers rooms not already taken
     (keeps the in-progress selection so a background refresh never drops it). */
  function rebuildRooms(cams) {
    const sel = gid("c-room");
    if (!sel) return;
    const cur = sel.value;
    const taken = new Set(cams.map(c => (c.room_name || "").toUpperCase()));
    const avail = ROOMS.filter(r => !taken.has(r.toUpperCase()) || r.toUpperCase() === cur.toUpperCase());
    const sig = cur + "|" + avail.join(",");
    if (sel.dataset.sig === sig) return;      // nothing changed — don't churn
    sel.dataset.sig = sig;
    sel.innerHTML = `<option value="">— select room —</option>` +
      avail.map(r => `<option${r.toUpperCase() === cur.toUpperCase() ? " selected" : ""}>${r}</option>`).join("");
  }

  function wireTable() {
    root.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
      try {
        await api("/api/v1/cameras/control", { method: "POST", body: JSON.stringify(
          { edgeId: await edgeId(), cameraLabel: b.dataset.id, action: b.dataset.act }) });
        toast(`Camera ${b.dataset.act}`); refresh();
      } catch (e) { toast(e.detail || "Control failed", "err"); }
    });
    root.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm(`Delete camera ${b.dataset.del}? This also removes it from your CERAVIS account.`)) return;
      await api("/api/v1/cameras/control", { method: "POST", body: JSON.stringify(
        { edgeId: await edgeId(), cameraLabel: b.dataset.del, action: "stop" }) }).catch(() => {});
      try {
        await api(`/api/v1/cameras/${b.dataset.del}`, { method: "DELETE" });  // edge also clears the cloud
        cvNotify("Camera removed", true);
      } catch (e) { cvNotify("Couldn't remove the camera — try again", false); }
      refresh();
    });
  }
  const ptzSend = (cam, body) => api(`/api/v1/cameras/${cam}/ptz`,
    { method: "POST", body: JSON.stringify(body) }).catch(() => {});

  /* ---- fullscreen live viewer (click a tile) + PTZ joystick box ---- */
  const MON_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>`;
  let fsOverlay = null, fsStream = null, fsEscHandler = null;

  function ptzBoxHtml(cam) {
    const btn = (spec, cls, glyph, title) =>
      `<button class="${cls}" data-cam="${cam}" data-ptz="${spec}" title="${title}">${glyph}</button>`;
    return `<div class="ptz-box" id="fs-ptz">
      <div class="ptz-h">PTZ CONTROL</div>
      <div class="ptz-hint">HOLD TO MOVE</div>
      <div class="ptz-dpad">
        ${btn("0,0.6,0", "up", "▲", "tilt up")}
        ${btn("0,-0.6,0", "down", "▼", "tilt down")}
        ${btn("-0.6,0,0", "left", "◀", "pan left")}
        ${btn("0.6,0,0", "right", "▶", "pan right")}
        ${btn("stop", "mid", "■", "stop")}
      </div>
      <div class="ptz-zoom">
        ${btn("0,0,-0.5", "", "－", "zoom out")}<span>ZOOM</span>${btn("0,0,0.5", "", "＋", "zoom in")}
      </div>
    </div>`;
  }
  function wirePtz(box) {
    box.querySelectorAll("[data-ptz]").forEach(b => {
      const cam = b.dataset.cam, spec = b.dataset.ptz;
      if (spec === "stop") { b.onclick = () => ptzSend(cam, { action: "stop" }); return; }
      const [pan, tilt, zoom] = spec.split(",").map(Number);
      b.onpointerdown = () => ptzSend(cam, { pan, tilt, zoom });
      b.onpointerup = b.onpointerleave = () => ptzSend(cam, { action: "stop" });
    });
  }
  function openFullscreen(cam) {
    closeFullscreen();
    const ov = el("div", "cam-fs");
    ov.innerHTML = `
      <div class="cam-fs-top">
        <div class="cam-fs-title">
          <div class="cam-fs-badge">${MON_ICON}</div>
          <div>
            <div class="cam-fs-name">${cam.room_name} · FULLSCREEN</div>
            <div class="cam-fs-sub">${cam.camera_name} · live preview</div>
          </div>
        </div>
        <div class="cam-fs-actions">
          ${cam.ptz_supported ? `<button class="cam-fs-btn on" id="fs-ptz-toggle" title="Toggle PTZ control">PTZ</button>` : ""}
          <button class="cam-fs-btn" id="fs-close" title="Close (Esc)">✕</button>
        </div>
      </div>
      <div class="cam-fs-stage">
        <div class="cam-fs-live"><span class="dot"></span>Live</div>
        <div class="nosignal" id="fs-nosig">CONNECTING…</div>
        <video id="fs-video"></video>
      </div>
      ${cam.ptz_supported ? ptzBoxHtml(cam.camera_id) : ""}`;
    document.body.appendChild(ov);
    fsOverlay = ov;
    const video = ov.querySelector("#fs-video");
    const nosig = ov.querySelector("#fs-nosig");
    video.style.display = "none";
    fsStream = liveView(video, cam.camera_id, {
      onState: s => {
        nosig.style.display = s === "live" ? "none" : "flex";
        video.style.display = s === "live" ? "block" : "none";
        nosig.textContent = s === "stalled" ? "SIGNAL STALLED"
          : s === "offline" ? "OFFLINE" : "CONNECTING…";
      },
    });
    ov.querySelector("#fs-close").onclick = closeFullscreen;
    const toggle = ov.querySelector("#fs-ptz-toggle");
    const box = ov.querySelector("#fs-ptz");
    if (toggle && box) {
      toggle.onclick = () => {
        box.classList.toggle("hidden");
        toggle.classList.toggle("on", !box.classList.contains("hidden"));
      };
      wirePtz(box);
    }
    fsEscHandler = (e) => { if (e.key === "Escape") closeFullscreen(); };
    document.addEventListener("keydown", fsEscHandler);
  }
  function closeFullscreen() {
    if (fsStream) { try { fsStream.stop(); } catch (_) {} fsStream = null; }
    if (fsEscHandler) { document.removeEventListener("keydown", fsEscHandler); fsEscHandler = null; }
    if (fsOverlay) { fsOverlay.remove(); fsOverlay = null; }
  }

  function syncWall(cams) {
    const grid = gid("c-grid");
    if (!grid) return;
    // matrix: 1 → 1 col, 2-4 → 2 cols (2×1 / 2×2), 5+ → 3 cols (3×2), scrolls beyond.
    const cols = cams.length <= 1 ? 1 : cams.length <= 4 ? 2 : 3;
    grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    const want = new Set(cams.map(c => c.camera_id));
    Object.keys(streams).forEach(id => {
      if (!want.has(id)) { streams[id].stop(); delete streams[id]; gid(`tile-${id}`)?.remove(); }
    });
    cams.forEach(c => {
      if (streams[c.camera_id]) return;
      const tile = el("div", "cam-tile");
      tile.id = `tile-${c.camera_id}`;
      tile.innerHTML = `
        <div class="nosignal">NO SIGNAL</div>
        <video style="display:none"></video>
        <div class="overlay"><span class="name">${c.camera_name}</span>
          <span class="room">· ${c.room_name}</span><span class="rec-dot"></span></div>
        <div class="foot"><span id="st-${c.camera_id}">connecting…</span></div>`;
      tile.title = "Open fullscreen · set the camera's angle";
      tile.onclick = () => openFullscreen(c);
      grid.appendChild(tile);
      const video = tile.querySelector("video");
      streams[c.camera_id] = liveView(video, c.camera_id, {
        onState: s => {
          tile.querySelector(".nosignal").style.display = s === "live" ? "none" : "flex";
          video.style.display = s === "live" ? "block" : "none";
          const st = gid(`st-${c.camera_id}`);
          if (st) st.textContent = s.toUpperCase();
        },
      });
    });
  }

  /* ---- ONVIF discovery ---- */
  async function runScan(deep) {
    const out = gid("d-scan-out");
    out.innerHTML = `<span class="muted">${deep ? "Deep scan — sweeping the subnet" : "Scanning the network"}…</span>`;
    gid("d-list").innerHTML = "";
    gid("d-creds").hidden = true;
    DISC.sel = DISC.probed = null;
    try {
      const r = await api(`/api/v1/discovery/scan${deep ? "?deep=1" : ""}`);
      DISC.cams = r.cameras || [];
      const where = (r.subnets || []).join(", ") || "this network";
      if (DISC.cams.length) {
        out.innerHTML = `<span class="ok">✓ ${DISC.cams.length} camera(s) found</span>
          <span class="faint">on ${where}</span>`;
      } else {
        out.innerHTML = `<span class="warn">No cameras answered on ${where}.</span>`
          + (deep ? "" : ` <button class="btn btn-ghost" style="padding:2px 8px;font-size:12px"
               id="d-deep">Deep scan</button>`);
        const deepBtn = gid("d-deep");
        if (deepBtn) deepBtn.onclick = () => runScan(true);
      }
      gid("d-list").innerHTML = DISC.cams.map((c, i) => `
        <label style="display:flex;gap:8px;align-items:center;padding:6px 2px;
                      font-size:13px;cursor:pointer">
          <input type="radio" name="d-cam" value="${i}" style="width:auto">
          <b>${c.name || c.hardware || c.manufacturer || "camera"}</b>
          <span class="faint">${c.ip}${c.hardware ? " · " + c.hardware : ""}${c.via === "unicast" ? " · sweep" : ""}</span>
        </label>`).join("");
      root.querySelectorAll('input[name="d-cam"]').forEach(elm => {
        elm.onchange = () => {
          DISC.sel = DISC.cams[parseInt(elm.value, 10)];
          DISC.probed = null;
          gid("d-creds").hidden = false;
          gid("d-probe-out").textContent = "";
        };
      });
    } catch (e) { out.innerHTML = `<span class="bad">✗ Scan failed — try again.</span>`; }
  }

  /* Auto-pick the best BROWSER-PLAYABLE stream: the largest H.264 profile (the
     backend recommends one at/below ~1080p). H.265 is skipped — no browser
     decodes HEVC over WebRTC. The AI keeps using the camera's main profile
     regardless; this choice is only what the /ui tiles and live links play. */
  function pickBestH264(r) {
    const h264 = (r.profiles || []).filter(p =>
      p.uri && (p.codec || p.encoding || "").toUpperCase() === "H264");
    if (!h264.length) return null;
    const rec = r.recommended || {};
    return h264.find(p => p.token === rec.token)
        || h264.slice().sort((a, b) => (b.width * b.height) - (a.width * a.height))[0];
  }

  /* ---- cloud save (saveCamera batch) ---- */
  async function syncCamerasRaw() {
    try { return await api("/api/v1/account/sync-cameras", { method: "POST" }); }
    catch (e) { return { synced: false, reason: "__unreachable__" }; }
  }
  async function syncCameras() {
    const r = await syncCamerasRaw();
    if (r && r.synced) cvNotify(`Saved · ${r.count} camera(s) synced to your account`, true);
    else cvNotify(r && r.reason === "__unreachable__"
      ? "Not saved — couldn't reach the server" : friendlyCloudError(r && r.reason), false);
    return r;
  }

  /* ---- wire the static controls ---- */
  gid("d-scan").onclick = () => runScan(false);

  gid("d-probe").onclick = async () => {                 // "Connect & preview"
    const out = gid("d-probe-out");
    if (!DISC.sel) return;
    out.innerHTML = `<span class="muted">Connecting…</span>`;
    try {
      const r = await api("/api/v1/discovery/probe", {
        method: "POST",
        body: JSON.stringify({
          xaddr: DISC.sel.xaddr,
          username: gid("d-user").value.trim(),
          password: gid("d-pass").value,
        }),
      });
      DISC.probed = r;
      const pick = pickBestH264(r);
      if (!pick) {
        DISC.profileToken = null;
        out.innerHTML = `<span class="bad">✗ This camera has no browser-playable (H.264) stream.</span>`;
        return;
      }
      DISC.profileToken = pick.token;
      gid("c-url").value = pick.uri || "";
      const dev = `${(r.device || {}).manufacturer || ""} ${(r.device || {}).model || ""}`.trim();
      out.innerHTML = `<span class="ok">✓ Connected${dev ? " · " + dev : ""}${r.ptz ? " · PTZ" : ""}</span>`
        + ` <span class="faint">— pick a room, then Save &amp; start.</span>`;
    } catch (e) {
      out.innerHTML = `<span class="bad">✗ ${friendlyProbeError(e)}</span>`;
    }
  };

  gid("c-save").onclick = async () => {                  // "Save & start"
    const cam = {
      room_name: gid("c-room").value.trim(),
      rtsp_url: gid("c-url").value.trim(),
      is_enabled: true,
    };
    const picked = DISC.probed && (DISC.probed.profiles || [])
      .find(p => p.token === DISC.profileToken);
    if (picked && cam.rtsp_url === picked.uri) {
      cam.onvif_xaddr = DISC.sel ? DISC.sel.xaddr : null;
      cam.onvif_username = gid("d-user").value.trim() || null;
      cam.onvif_password = gid("d-pass").value || null;
      cam.onvif_profile_token = picked.token || null;
      const ai = DISC.probed.ai || {};
      const wantsAi = ai.uri && ai.token && ai.token !== picked.token;
      cam.ai_rtsp_url = wantsAi ? ai.uri : "";
      cam.ai_profile_token = wantsAi ? ai.token : null;
      cam.ptz_supported = !!DISC.probed.ptz;
    }
    if (!cam.room_name) return cvNotify("Pick a room for this camera.", false);
    if (!cam.rtsp_url) return cvNotify("Connect to a camera (or paste an RTSP URL) first.", false);
    const btn = gid("c-save");
    btn.disabled = true;
    try {
      const res = await api("/api/v1/cameras", { method: "POST", body: JSON.stringify(cam) });
      const cid = (res && res.camera_id) || "";
      if (cid) await api("/api/v1/cameras/control", { method: "POST", body: JSON.stringify(
        { edgeId: await edgeId(), cameraLabel: cid, action: "start" }) }).catch(() => {});
      // reset the add form
      gid("c-url").value = ""; gid("c-room").value = "";
      gid("d-probe-out").textContent = "";
      DISC.probed = DISC.sel = DISC.profileToken = null;
      gid("d-creds").hidden = true;
      await refresh();
      if (opts.cloudSaveOnAdd) {
        const r = await syncCamerasRaw();               // cameras.html saves to the account now
        if (r && r.synced)
          cvNotify("Saved — open the tile to set the camera's angle.", true);
        else
          cvNotify(r && r.reason === "__unreachable__"
            ? "Started, but not saved — couldn't reach the server" : friendlyCloudError(r && r.reason), false);
      } else {
        cvNotify("Preview ready — open the tile to set the camera's angle.", true);
      }
    } catch (e) {
      const d = ((e && (e.detail || e.message)) || "").toString().toLowerCase();
      cvNotify(d.includes("already") || d.includes("exists")
        ? "This room already has a camera." : "Couldn't save the camera — try again.", false);
    } finally { btn.disabled = false; }
  };

  function destroy() {
    destroyed = true;
    if (refreshTimer) clearInterval(refreshTimer);
    Object.values(streams).forEach(s => { try { s.stop(); } catch (_) {} });
    closeFullscreen();
    root.innerHTML = "";
  }

  refresh();
  refreshTimer = setInterval(refresh, 5000);
  return { refresh, syncCameras, destroy };
}
window.mountCameras = mountCameras;
