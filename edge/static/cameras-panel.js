/* =====================================================================
   Shared camera-management panel — ONE implementation used by BOTH the
   setup wizard (Step 2) and the dedicated cameras.html page.
   mountCameras(root) renders: ONVIF discovery + profile pick, manual RTSP
   add, a registered-cameras table (start/stop/restart/delete + PTZ), a live
   verification wall, and a "sync to CERAVIS" action. Returns
   { refresh, syncCameras, destroy }. Depends on ceravis.js (api, toast, el,
   healthPill) and live-view.js (liveView), which every page loads first.
   ===================================================================== */
function mountCameras(root, opts = {}) {
  const CAM_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg>`;

  root.innerHTML = `
  <div class="cam-panel">
    <div class="split">
      <div class="card">
        <h2>Find cameras <span class="faint">(WiFi / ONVIF)</span></h2>
        <div class="hint mb">Scans every network interface — WiFi, ethernet and the
          device hotspot — and fills the form below automatically. If discovery is
          blocked it sweeps the subnet as a fallback. Manual RTSP entry works too.</div>
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
            <button class="btn btn-ghost" id="d-probe">Connect &amp; read profiles</button>
            <span class="probe-result" id="d-probe-out"></span>
          </div>
          <div id="d-profiles" class="mt" hidden>
            <label class="f">Stream to use</label>
            <div class="hint" style="margin-bottom:6px">This ONE stream feeds the AI,
              the recordings and every live view, so it is chosen once here. The
              recommended pick is already selected.</div>
            <div id="d-profile-list"></div>
            <div class="probe-result" id="d-profile-why"></div>
          </div>
        </div>
        <hr class="sep" />
        <h2>Add a camera</h2>
        <label class="f">RTSP stream URL</label>
        <input type="text" id="c-url" placeholder="rtsp://user:pass@192.168.x.x:554/stream1" />
        <div class="hint">Special characters in the password (<code>@ : /</code>) are
          percent-encoded for you by Test connection — keep the encoded form.
          4K (H.265) mains stream over TCP automatically.</div>
        <div class="row mt">
          <button class="btn btn-ghost" id="c-probe">Test connection</button>
          <span class="probe-result" id="c-probe-out"></span>
        </div>
        <label class="f">Camera label</label>
        <select id="c-room">
          <option value="">— select room —</option>
          <option>KITCHEN</option><option>BEDROOM</option><option>LIVING ROOM</option>
          <option>LOUNGE</option><option>LIVE FEED</option>
        </select>
        <div class="hint">The room this camera watches. Its id and live link are
          derived from the label automatically on save.</div>
        <div class="row mt">
          <button class="btn btn-primary" id="c-save">Save &amp; start camera</button>
        </div>
        <hr class="sep" />
        <h2>Registered cameras</h2>
        <table class="tbl"><tbody id="c-table"></tbody></table>
        <div class="row mt">
          <button class="btn btn-ghost btn-sm" id="c-sync">Sync to CERAVIS account</button>
          <span class="probe-result" id="c-sync-out"></span>
        </div>
      </div>

      <div class="card">
        <h2>Live verification wall</h2>
        <div class="hint mb">Streams appear here as soon as a camera is saved —
          verify aim and coverage.</div>
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

  /* ---- registered cameras (table + controls + PTZ) + live wall ---- */
  async function refresh() {
    let cams, status;
    try {
      [cams, status] = await Promise.all([
        api("/api/v1/cameras"), api("/api/v1/cameras/status").catch(() => ({})),
      ]);
    } catch { return; }
    if (destroyed) return;
    const tbody = gid("c-table");
    if (!tbody) return;
    tbody.innerHTML = cams.length === 0
      ? `<tr><td colspan="3"><div class="empty">
           <div class="empty-ic">${CAM_ICON}</div>
           <div class="empty-title">No cameras added yet</div>
           <div class="empty-sub">Scan the network above, or paste an RTSP URL and save your first camera.</div>
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
              ${c.ptz_supported ? `<button class="btn btn-ghost btn-sm" data-ptztoggle="${c.camera_id}">PTZ</button>` : ""}
              <button class="btn btn-danger btn-sm" data-del="${c.camera_id}">Delete</button>
            </td></tr>
            ${c.ptz_supported ? `<tr class="ptz-row" id="ptz-${c.camera_id}" hidden><td colspan="3">${ptzPadHtml(c.camera_id)}</td></tr>` : ""}`;
        }).join("");
    wireTable();
    syncWall(cams);
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
      if (!confirm(`Delete camera ${b.dataset.del}?`)) return;
      await api("/api/v1/cameras/control", { method: "POST", body: JSON.stringify(
        { edgeId: await edgeId(), cameraLabel: b.dataset.del, action: "stop" }) }).catch(() => {});
      await api(`/api/v1/cameras/${b.dataset.del}`, { method: "DELETE" });
      toast("Camera removed"); refresh();
    });
    root.querySelectorAll("[data-ptztoggle]").forEach(b => b.onclick = () => {
      const rowEl = gid("ptz-" + b.dataset.ptztoggle);
      if (rowEl) rowEl.hidden = !rowEl.hidden;
    });
    root.querySelectorAll(".ptz-btn").forEach(btn => {
      const cam = btn.dataset.cam;
      const [pan, tilt, zoom] = btn.dataset.ptz.split(",").map(Number);
      btn.onpointerdown = () => ptzSend(cam, { pan, tilt, zoom });
      btn.onpointerup = btn.onpointerleave = () => ptzSend(cam, { action: "stop" });
    });
  }
  const ptzSend = (cam, body) => api(`/api/v1/cameras/${cam}/ptz`,
    { method: "POST", body: JSON.stringify(body) }).catch(() => {});
  function ptzPadHtml(cam) {
    const b = (p, t, z, label, title) =>
      `<button class="btn btn-ghost btn-sm ptz-btn" data-cam="${cam}" data-ptz="${p},${t},${z}" title="${title}">${label}</button>`;
    return `<div class="ptz-pad">
      ${b(0, 0.6, 0, "▲", "tilt up")}${b(0, -0.6, 0, "▼", "tilt down")}
      ${b(-0.6, 0, 0, "◀", "pan left")}${b(0.6, 0, 0, "▶", "pan right")}
      ${b(0, 0, 0.5, "＋", "zoom in")}${b(0, 0, -0.5, "－", "zoom out")}
      <span class="faint" style="align-self:center">hold to move</span></div>`;
  }

  function syncWall(cams) {
    const grid = gid("c-grid");
    if (!grid) return;
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
        out.innerHTML = `<span class="warn">No ONVIF cameras answered on ${where}.</span>`
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
    } catch (e) { out.innerHTML = `<span class="bad">✗ scan failed</span>`; }
  }

  function selectProfile(token) {
    const r = DISC.probed;
    if (!r) return;
    const p = (r.profiles || []).find(x => x.token === token);
    if (!p) return;
    DISC.profileToken = token;
    gid("c-url").value = p.uri || "";
    root.querySelectorAll(".prof-row").forEach(elm =>
      elm.classList.toggle("sel", elm.dataset.token === token));
    const ai = r.ai || {};
    const aiNote = ai.uri && ai.token !== token
      ? `<br><span class="muted">The AI will additionally read
         ${ai.resolution} ${ai.encoding} — more reach for tracking. This camera is
         the only kind that gets two streams.</span>` : "";
    const enc = (p.codec || p.encoding || "").toUpperCase();
    gid("d-profile-why").innerHTML = enc && enc !== "H264"
      ? `<span class="bad">${enc} cannot play in any browser — live view will be
         black and its recordings unplayable.</span>`
      : `<span class="muted">${(r.recommended || {}).reason || ""}</span>${aiNote}`;
  }

  function renderProfiles(r) {
    const wrap = gid("d-profiles");
    const list = gid("d-profile-list");
    const rec = r.recommended || {};
    const profiles = (r.profiles || [])
      .filter(p => p.uri && (p.codec || p.encoding || "").toUpperCase() !== "JPEG");
    if (!profiles.length) { wrap.hidden = true; return; }
    list.innerHTML = profiles.map(p => {
      const enc = (p.codec || p.encoding || "").toUpperCase();
      const bad = enc && enc !== "H264";
      return `<div class="prof-row" data-token="${p.token}">
        <span class="res">${p.width}×${p.height}</span>
        <span class="tag ${bad ? "bad" : ""}">${enc || "?"}</span>
        <span class="muted">${p.fps ? Math.round(p.fps) + " fps" : ""} · ${p.name || p.token}</span>
        ${p.token === rec.token ? '<span class="pick">RECOMMENDED</span>' : ""}
      </div>`;
    }).join("");
    list.querySelectorAll(".prof-row").forEach(elm =>
      elm.onclick = () => selectProfile(elm.dataset.token));
    wrap.hidden = false;
    selectProfile(rec.token || profiles[0].token);
  }

  /* ---- cloud sync ---- */
  async function syncCameras(manual) {
    const out = gid("c-sync-out");
    if (out) out.innerHTML = `<span class="muted">Syncing…</span>`;
    try {
      const r = await api("/api/v1/account/sync-cameras", { method: "POST" });
      if (r.synced) {
        if (out) out.innerHTML = `<span class="ok">✓ ${r.count} camera(s) synced to CERAVIS</span>`;
        if (manual) toast(`${r.count} camera(s) sent to CERAVIS`);
      } else {
        if (out) out.innerHTML = `<span class="bad">✗ ${r.reason || "sync failed"}</span>`;
        if (manual) toast(r.reason || "Camera sync failed", "err");
      }
      return r;
    } catch (e) { if (out) out.innerHTML = `<span class="bad">✗ sync failed</span>`; return null; }
  }

  /* ---- wire the static controls ---- */
  gid("d-scan").onclick = () => runScan(false);
  gid("d-probe").onclick = async () => {
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
      out.innerHTML = `<span class="ok">✓ ${r.device.manufacturer || ""} ${r.device.model || ""}
        ${r.ptz ? " · PTZ ✓" : ""}</span>`;
      renderProfiles(r);
    } catch (e) {
      out.innerHTML = `<span class="bad">✗ ${e.detail || "connection failed (credentials?)"}</span>`;
    }
  };
  gid("c-probe").onclick = async () => {
    const out = gid("c-probe-out");
    const field = gid("c-url");
    const url = field.value.trim();
    if (!url) { out.innerHTML = `<span class="warn">Enter an RTSP URL first.</span>`; return; }
    out.innerHTML = `<span class="muted">Probing stream (TCP)…</span>`;
    try {
      const r = await api("/api/v1/cameras/probe", {
        method: "POST", body: JSON.stringify({ rtsp_url: url }) });
      if (r.url && r.url !== url) field.value = r.url;
      const fixed = (r.url && r.url !== url)
        ? ` <span class="faint">(credentials auto-encoded)</span>` : "";
      out.innerHTML = r.ok
        ? `<span class="ok">✓ Connected — ${r.width}×${r.height}</span>${fixed}`
        : `<span class="bad">✗ ${r.reason}</span>${fixed}`;
    } catch (e) { out.innerHTML = `<span class="bad">✗ probe failed</span>`; }
  };
  gid("c-save").onclick = async () => {
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
    if (!cam.room_name || !cam.rtsp_url)
      return toast("Pick a camera label and enter (or test) an RTSP URL", "err");
    try {
      const res = await api("/api/v1/cameras", { method: "POST", body: JSON.stringify(cam) });
      const cid = (res && res.camera_id) || "";
      if (cid) await api("/api/v1/cameras/control", { method: "POST", body: JSON.stringify(
        { edgeId: await edgeId(), cameraLabel: cid, action: "start" }) });
      toast(`${cam.room_name} saved & started`);
      ["c-url", "c-room"].forEach(i => gid(i).value = "");
      gid("c-probe-out").textContent = "";
      DISC.probed = DISC.sel = DISC.profileToken = null;
      gid("d-creds").hidden = true;
      gid("d-profiles").hidden = true;
      gid("d-probe-out").textContent = "";
      refresh();
    } catch (e) { toast(e.detail || "Save failed", "err"); }
  };
  gid("c-sync").onclick = () => syncCameras(true);

  function destroy() {
    destroyed = true;
    if (refreshTimer) clearInterval(refreshTimer);
    Object.values(streams).forEach(s => { try { s.stop(); } catch (_) {} });
    root.innerHTML = "";
  }

  refresh();
  refreshTimer = setInterval(refresh, 5000);
  return { refresh, syncCameras, destroy };
}
window.mountCameras = mountCameras;
