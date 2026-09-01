/* =====================================================================
   Shared zone-labeler panel — ONE implementation used by BOTH the setup
   wizard (Step 3) and the dedicated zones.html page. mountZones(root)
   renders the camera picker, polygon drawing canvas, per-camera zone list
   and save/cloud-finalize actions. Returns { refresh, finalize, destroy }.
   Depends on ceravis.js (api, toast), loaded first.
   ===================================================================== */
function mountZones(root, opts = {}) {
  root.innerHTML = `
  <div class="zone-panel">
    <div class="split">
      <div class="card">
        <h2>Zone the rooms</h2>
        <div class="hint mb">Pick a camera, then click on the image to outline an area
          (bed, fridge, door…). Double-click — or "Close polygon" — to finish the shape.
          Drag any point to adjust. Zones give alerts spatial meaning:
          <i>"fall near the bed"</i>.</div>
        <label class="f">Camera</label>
        <select id="z-cam"></select>
        <label class="f">New zone name</label>
        <input type="text" id="z-name" placeholder="bed / fridge / door" />
        <div class="row mt">
          <button class="btn btn-primary btn-sm" id="z-close">Close polygon</button>
          <button class="btn btn-ghost btn-sm" id="z-undo">Undo point</button>
          <button class="btn btn-ghost btn-sm" id="z-clear">Discard drawing</button>
          <button class="btn btn-ghost btn-sm" id="z-refresh">Refresh frame</button>
        </div>
        <hr class="sep" />
        <h2>Zones on this camera</h2>
        <div id="z-list"></div>
        <div class="row mt">
          <button class="btn btn-primary" id="z-save">Save all zones</button>
          <button class="btn btn-ghost" id="z-finalize">Sync zones to account</button>
        </div>
      </div>
      <div class="card">
        <h2>Camera view</h2>
        <div class="zone-stage"><canvas id="z-canvas" width="960" height="540"></canvas></div>
        <div class="hint mt" id="z-status">Select a camera to begin.</div>
      </div>
    </div>
  </div>`;

  const gid = (id) => document.getElementById(id);
  const Z = { cam: null, img: null, zones: [], drawing: [], sel: -1, drag: null };
  const zCanvas = gid("z-canvas");
  const zCtx = zCanvas.getContext("2d");
  const COLORS = ["#2dd4bf", "#60a5fa", "#fbbf24", "#f87171", "#c084fc", "#34d399", "#fb923c"];

  async function refresh() {
    const cams = await api("/api/v1/cameras").catch(() => []);
    const sel = gid("z-cam");
    sel.innerHTML = `<option value="">— select camera —</option>` +
      cams.map(c => `<option value="${c.camera_id}">${c.camera_name} (${c.room_name})</option>`).join("");
    sel.onchange = () => loadZoneCam(sel.value);
  }

  async function loadZoneCam(camId) {
    Z.cam = camId; Z.drawing = []; Z.sel = -1;
    if (!camId) { Z.img = null; zDraw(); return; }
    gid("z-status").textContent = "Loading frame…";
    try {
      const blob = await fetch(`/api/v1/cameras/${camId}/snapshot?quality=75&t=${Date.now()}`)
        .then(r => { if (!r.ok) throw 0; return r.blob(); });
      const img = new Image();
      img.onload = () => {
        Z.img = img; zCanvas.width = img.naturalWidth; zCanvas.height = img.naturalHeight;
        zDraw();
        gid("z-status").textContent =
          "Click to add points · double-click to close · drag a point to adjust.";
      };
      img.src = URL.createObjectURL(blob);
    } catch {
      Z.img = null; zDraw();
      gid("z-status").innerHTML =
        `<span class="warn">No frame available — is the camera running?</span>`;
    }
    Z.zones = (await api(`/api/v1/zones?camera_id=${encodeURIComponent(camId)}`)) || [];
    zList(); zDraw();
  }
  function zDraw() {
    zCtx.fillStyle = "#000"; zCtx.fillRect(0, 0, zCanvas.width, zCanvas.height);
    if (Z.img) zCtx.drawImage(Z.img, 0, 0, zCanvas.width, zCanvas.height);
    Z.zones.forEach((z, i) => zPoly(z.polygon, COLORS[i % COLORS.length],
      z.zone_name, false, i === Z.sel));
    if (Z.drawing.length) zPoly(Z.drawing, "#ffffff", "…", true, false);
  }
  function zPoly(pts, color, label, open, selected) {
    if (!pts.length) return;
    zCtx.lineWidth = selected ? 4 : 2.5; zCtx.strokeStyle = color;
    zCtx.fillStyle = color + "2e";
    zCtx.beginPath(); zCtx.moveTo(pts[0][0], pts[0][1]);
    pts.slice(1).forEach(p => zCtx.lineTo(p[0], p[1]));
    if (!open) { zCtx.closePath(); zCtx.fill(); }
    zCtx.stroke();
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
    for (let zi = 0; zi < Z.zones.length; zi++)
      for (const [px, py] of Z.zones[zi].polygon)
        if ((px - x) ** 2 + (py - y) ** 2 < 144) { Z.sel = zi; zList(); zDraw(); return; }
    Z.drawing.push([x, y]); zDraw();
  });
  zCanvas.addEventListener("dblclick", () => closeDrawing());
  function closeDrawing() {
    if (Z.drawing.length < 3) return toast("A zone needs at least 3 points", "err");
    const name = gid("z-name").value.trim();
    if (!name) return toast("Name the zone first", "err");
    Z.zones.push({ zone_id: "zone_" + Math.random().toString(36).slice(2, 8),
                   camera_id: Z.cam, zone_name: name, polygon: Z.drawing.slice() });
    Z.drawing = []; gid("z-name").value = "";
    zList(); zDraw();
  }
  gid("z-close").onclick = closeDrawing;
  gid("z-undo").onclick = () => { Z.drawing.pop(); zDraw(); };
  gid("z-clear").onclick = () => { Z.drawing = []; zDraw(); };
  gid("z-refresh").onclick = () => Z.cam && loadZoneCam(Z.cam);
  function zList() {
    gid("z-list").innerHTML = Z.zones.length === 0
      ? `<div class="faint">No zones yet for this camera.</div>`
      : Z.zones.map((z, i) => `
        <div class="zone-list-item ${i === Z.sel ? "sel" : ""}" data-zi="${i}">
          <span class="zone-chip" style="background:${COLORS[i % COLORS.length]}"></span>
          <input type="text" value="${z.zone_name}" data-rn="${i}"
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
  gid("z-save").onclick = async () => {
    if (!Z.cam) return toast("Select a camera first", "err");
    try {
      await api(`/api/v1/zones/camera/${Z.cam}`,
        { method: "PUT", body: JSON.stringify(Z.zones) });
      toast(`Saved ${Z.zones.length} zones for ${Z.cam}`);
    } catch (e) { toast("Save failed", "err"); }
  };
  gid("z-finalize").onclick = () => finalize(true);

  /* Upload the single consolidated zones_<id>.json (all cameras) to the account. */
  async function finalize(manual) {
    try {
      const z = await api("/api/v1/zones/finalize", { method: "POST" });
      if (z && z.uploaded)
        toast(`Zones saved to your account · ${z.cameras} camera(s), ${z.zones} zone(s)`);
      else if (z && z.reason && manual)
        toast(`Zones not uploaded: ${z.reason}`, "err");
      return z;
    } catch (e) { return null; }
  }

  function destroy() {
    window.removeEventListener("mouseup", onUp);
    root.innerHTML = "";
  }

  refresh();
  return { refresh, finalize, destroy };
}
window.mountZones = mountZones;
