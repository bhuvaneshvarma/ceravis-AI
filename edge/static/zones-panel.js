/* =====================================================================
   Shared zone-labeler panel — ONE implementation used by BOTH the setup
   wizard (Step 3) and the dedicated zones.html page.
   mountZones(root, { showSaveAll }) renders the camera picker, an animated
   polygon-drawing canvas (the shape forms live as points are dropped; it
   closes itself at 3+ points — no "close polygon" button), a per-camera zone
   list, a "Save to <camera>" action (commit + persist this camera's zones),
   and — on the standalone page only — a "Save all zones" action that writes
   the whole zones.json and uploads to the account. Returns
   { refresh, finalize, destroy }. Depends on ceravis.js (api, toast, cvNotify,
   prettyLabel), loaded first.
   ===================================================================== */
function mountZones(root, opts = {}) {
  root.innerHTML = `
  <div class="zone-panel">
    <div class="split">
      <div class="card">
        <h2>Zone the rooms</h2>
        <div class="hint mb">Pick a camera and name the area, then click on the frame to
          drop points — the zone shape forms and closes itself as you go.</div>
        <label class="f">Camera</label>
        <select id="z-cam"></select>
        <label class="f">Zone name</label>
        <input type="text" id="z-name" placeholder="bed / fridge / door" />
        <div class="row mt">
          <button class="btn btn-ghost btn-sm" id="z-refresh">Refresh frame</button>
          <button class="btn btn-ghost btn-sm" id="z-undo">Undo point</button>
          <button class="btn btn-ghost btn-sm" id="z-clear">Discard</button>
        </div>
        <div class="row mt"><button class="btn btn-primary btn-sm" id="z-savecam">Save zone</button></div>
        <hr class="sep" />
        <h2>Zones on this camera</h2>
        <div id="z-list"></div>
        ${opts.showSaveAll
          ? `<div class="row mt"><button class="btn btn-teal btn-sm" id="z-saveall">Save all zones</button></div>`
          : ""}
      </div>
      <div class="card">
        <h2>Camera view</h2>
        <div class="zone-stage"><canvas id="z-canvas" width="960" height="540"></canvas></div>
        <div class="hint mt" id="z-status">Select a camera to begin.</div>
      </div>
    </div>
  </div>`;

  const gid = (id) => document.getElementById(id);
  const Z = { cam: null, camName: "", img: null, zones: [], drawing: [], sel: -1, drag: null };
  const zCanvas = gid("z-canvas");
  const zCtx = zCanvas.getContext("2d");
  const COLORS = ["#2dd4bf", "#60a5fa", "#fbbf24", "#f87171", "#c084fc", "#34d399", "#fb923c"];
  const escAttr = (s) => String(s == null ? "" : s).replace(/"/g, "&quot;");
  let CAMS = [];

  async function refresh() {
    CAMS = await api("/api/v1/cameras").catch(() => []);
    const sel = gid("z-cam");
    if (!sel) return;
    sel.innerHTML = `<option value="">— select camera —</option>` +
      CAMS.map(c => `<option value="${c.camera_id}">${prettyLabel(c.camera_name)} (${prettyLabel(c.room_name)})</option>`).join("");
    sel.onchange = () => loadZoneCam(sel.value);
  }

  function updateSaveLabel() {
    const b = gid("z-savecam");
    if (b) b.textContent = Z.camName ? `Save to ${Z.camName}` : "Save zone";
  }

  async function loadZoneCam(camId) {
    Z.cam = camId; Z.drawing = []; Z.sel = -1; stopDrawAnim();
    const cam = CAMS.find(c => c.camera_id === camId);
    Z.camName = cam ? prettyLabel(cam.room_name) : "";
    updateSaveLabel();
    if (!camId) { Z.img = null; zDraw(); gid("z-status").textContent = "Select a camera to begin."; return; }
    gid("z-status").textContent = "Loading frame…";
    try {
      const blob = await fetch(`/api/v1/cameras/${camId}/snapshot?quality=75&t=${Date.now()}`)
        .then(r => { if (!r.ok) throw 0; return r.blob(); });
      const img = new Image();
      img.onload = () => {
        Z.img = img; zCanvas.width = img.naturalWidth; zCanvas.height = img.naturalHeight;
        zDraw();
        gid("z-status").textContent = "Name the area, then click to drop points — 3+ closes the zone.";
      };
      img.src = URL.createObjectURL(blob);
    } catch {
      Z.img = null; zDraw();
      gid("z-status").innerHTML = `<span class="warn">No frame available — is the camera running?</span>`;
    }
    Z.zones = (await api(`/api/v1/zones?camera_id=${encodeURIComponent(camId)}`)) || [];
    zList(); zDraw();
  }

  /* ---- rendering (committed zones are static; the active drawing animates) ---- */
  function zDraw(now) {
    zCtx.fillStyle = "#000"; zCtx.fillRect(0, 0, zCanvas.width, zCanvas.height);
    if (Z.img) zCtx.drawImage(Z.img, 0, 0, zCanvas.width, zCanvas.height);
    Z.zones.forEach((z, i) => zPoly(z.polygon, COLORS[i % COLORS.length], z.zone_name, i === Z.sel));
    drawActive(now);
  }
  function zPoly(pts, color, label, selected) {
    if (!pts.length) return;
    zCtx.lineWidth = selected ? 4 : 2.5; zCtx.strokeStyle = color; zCtx.fillStyle = color + "2e";
    zCtx.beginPath(); zCtx.moveTo(pts[0][0], pts[0][1]);
    pts.slice(1).forEach(p => zCtx.lineTo(p[0], p[1]));
    zCtx.closePath(); zCtx.fill(); zCtx.stroke();
    pts.forEach(p => { zCtx.beginPath(); zCtx.arc(p[0], p[1], selected ? 7 : 5, 0, 7);
      zCtx.fillStyle = color; zCtx.fill(); });
    if (label) {
      zCtx.font = "600 15px sans-serif";
      const w = zCtx.measureText(label).width;
      zCtx.fillStyle = "rgba(0,0,0,.75)";
      zCtx.fillRect(pts[0][0] + 8, pts[0][1] - 26, w + 14, 22);
      zCtx.fillStyle = color; zCtx.fillText(label, pts[0][0] + 15, pts[0][1] - 10);
    }
  }
  /* The zone being drawn: connecting lines the moment there's a 2nd point, a
     filled closed shape once there are 3, marching-ants outline + a ripple on
     the freshest dot — a premium "forming live" feel. */
  function drawActive(now) {
    const pts = Z.drawing;
    if (!pts.length) return;
    const teal = "#2dd4bf";
    if (pts.length >= 2) {
      zCtx.beginPath(); zCtx.moveTo(pts[0][0], pts[0][1]);
      pts.slice(1).forEach(p => zCtx.lineTo(p[0], p[1]));
      if (pts.length >= 3) { zCtx.closePath(); zCtx.fillStyle = "rgba(45,212,191,.18)"; zCtx.fill(); }
      zCtx.save();
      zCtx.setLineDash([7, 6]); zCtx.lineDashOffset = now ? -(now / 55) % 13 : 0;
      zCtx.lineWidth = 2.5; zCtx.strokeStyle = teal; zCtx.stroke();
      zCtx.restore();
    }
    pts.forEach((p, i) => {
      const fresh = i === pts.length - 1;
      zCtx.beginPath(); zCtx.arc(p[0], p[1], fresh ? 6 : 5, 0, 7);
      zCtx.fillStyle = "#fff"; zCtx.fill();
      zCtx.lineWidth = 2; zCtx.strokeStyle = teal; zCtx.stroke();
    });
    if (now && lastDotT) {                       // expanding ripple on the newest dot
      const t = (now - lastDotT) / 450;
      if (t >= 0 && t <= 1) {
        const last = pts[pts.length - 1];
        zCtx.beginPath(); zCtx.arc(last[0], last[1], 6 + t * 22, 0, 7);
        zCtx.strokeStyle = `rgba(45,212,191,${(1 - t) * 0.6})`; zCtx.lineWidth = 2; zCtx.stroke();
      }
    }
  }
  let drawAnimRAF = null, lastDotT = 0;
  function startDrawAnim() {
    if (drawAnimRAF) return;
    const loop = () => { zDraw(performance.now()); drawAnimRAF = requestAnimationFrame(loop); };
    drawAnimRAF = requestAnimationFrame(loop);
  }
  function stopDrawAnim() {
    if (drawAnimRAF) { cancelAnimationFrame(drawAnimRAF); drawAnimRAF = null; }
    zDraw();
  }

  function zPos(e) {
    const r = zCanvas.getBoundingClientRect();
    return [Math.round((e.clientX - r.left) * zCanvas.width / r.width),
            Math.round((e.clientY - r.top) * zCanvas.height / r.height)];
  }
  zCanvas.addEventListener("mousedown", e => {
    const [x, y] = zPos(e);
    for (let zi = 0; zi < Z.zones.length; zi++)
      for (let pi = 0; pi < Z.zones[zi].polygon.length; pi++) {
        const [px, py] = Z.zones[zi].polygon[pi];
        if ((px - x) ** 2 + (py - y) ** 2 < 144) { Z.drag = [zi, pi]; Z.sel = zi; zList(); return; }
      }
  });
  zCanvas.addEventListener("mousemove", e => {
    if (!Z.drag) return;
    const [x, y] = zPos(e);
    Z.zones[Z.drag[0]].polygon[Z.drag[1]] = [x, y]; zDraw();
  });
  const onUp = () => { Z.drag = null; };
  window.addEventListener("mouseup", onUp);
  zCanvas.addEventListener("click", e => {
    if (Z.drag) return;
    const [x, y] = zPos(e);
    for (let zi = 0; zi < Z.zones.length; zi++)         // click an existing zone → select it
      for (const [px, py] of Z.zones[zi].polygon)
        if ((px - x) ** 2 + (py - y) ** 2 < 144) { Z.sel = zi; zList(); zDraw(); return; }
    // Can't drop points without an active frame (camera) and a zone name.
    if (!Z.cam) return toast("Select a camera first.", "err");
    if (!gid("z-name").value.trim()) return toast("Name the zone first.", "err");
    Z.drawing.push([x, y]); lastDotT = performance.now(); startDrawAnim();
  });

  gid("z-undo").onclick = () => { Z.drawing.pop(); if (!Z.drawing.length) stopDrawAnim(); else zDraw(); };
  gid("z-clear").onclick = () => { Z.drawing = []; stopDrawAnim(); };
  gid("z-refresh").onclick = () => Z.cam && loadZoneCam(Z.cam);

  function zList() {
    gid("z-list").innerHTML = Z.zones.length === 0
      ? `<div class="faint">No zones yet for this camera.</div>`
      : Z.zones.map((z, i) => `
        <div class="zone-list-item ${i === Z.sel ? "sel" : ""}" data-zi="${i}">
          <span class="zone-chip" style="background:${COLORS[i % COLORS.length]}"></span>
          <input type="text" value="${escAttr(z.zone_name)}" data-rn="${i}"
                 style="flex:1;padding:4px 8px;font-size:13px" />
          <span class="faint">${z.polygon.length} pts</span>
          <button class="btn btn-danger btn-sm" data-zdel="${i}">✕</button>
        </div>`).join("");
    root.querySelectorAll("[data-zi]").forEach(d =>
      d.onclick = e => { if (e.target.tagName === "DIV" || e.target.tagName === "SPAN")
        { Z.sel = +d.dataset.zi; zList(); zDraw(); } });
    root.querySelectorAll("[data-rn]").forEach(inp =>
      inp.onchange = () => { Z.zones[+inp.dataset.rn].zone_name = inp.value.trim(); zDraw(); });
    root.querySelectorAll("[data-zdel]").forEach(b =>
      b.onclick = () => { Z.zones.splice(+b.dataset.zdel, 1); Z.sel = -1; zList(); zDraw(); });
  }

  /* Commit the drawn polygon (if any) into this camera's list, then persist the
     camera's zones. This is the per-camera save; it does NOT hit the cloud. */
  async function saveCurrentCamera() {
    if (!Z.cam) { cvNotify("Select a camera first.", false); return false; }
    if (Z.drawing.length) {
      if (Z.drawing.length < 3) { toast("A zone needs at least 3 points (or discard it).", "err"); return false; }
      const name = gid("z-name").value.trim();
      if (!name) { toast("Name the zone first.", "err"); return false; }
      Z.zones.push({ zone_id: "zone_" + Math.random().toString(36).slice(2, 8),
                     camera_id: Z.cam, zone_name: name, polygon: Z.drawing.slice() });
      Z.drawing = []; gid("z-name").value = ""; stopDrawAnim(); zList();
    }
    try {
      await api(`/api/v1/zones/camera/${Z.cam}`, { method: "PUT", body: JSON.stringify(Z.zones) });
      return true;
    } catch (e) { cvNotify("Couldn't save zones — try again.", false); return false; }
  }
  gid("z-savecam").onclick = async () => {
    if (await saveCurrentCamera())
      cvNotify(`Saved to ${Z.camName || prettyLabel(Z.cam)} · ${Z.zones.length} zone(s)`, true);
  };

  /* Persist the current camera, then write the consolidated zones.json for ALL
     cameras and upload it to the account (the embeddings/zoning cloud call). Used
     by the standalone page's "Save all zones" and the wizard's Continue. */
  async function finalize(manual) {
    if (Z.cam) await saveCurrentCameraSilent();
    try {
      const z = await api("/api/v1/zones/finalize", { method: "POST" });
      if (z && z.uploaded)
        cvNotify(`Zones saved to your account · ${z.cameras} camera(s), ${z.zones} zone(s)`, true);
      else if (manual)
        cvNotify(z && z.reason ? `Not saved: ${z.reason}` : "Saved locally (not uploaded).", false);
      return z;
    } catch (e) { if (manual) cvNotify("Couldn't save zones to your account.", false); return null; }
  }
  async function saveCurrentCameraSilent() {
    try { await api(`/api/v1/zones/camera/${Z.cam}`, { method: "PUT", body: JSON.stringify(Z.zones) }); }
    catch (_) {}
  }
  const saveAll = gid("z-saveall");
  if (saveAll) saveAll.onclick = () => finalize(true);

  function destroy() {
    stopDrawAnim();
    window.removeEventListener("mouseup", onUp);
    root.innerHTML = "";
  }

  refresh();
  return { refresh, finalize, destroy };
}
window.mountZones = mountZones;
