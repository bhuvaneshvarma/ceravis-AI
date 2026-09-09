/* The talk-back control on a camera tile: the button, its states, and the
   one-time commissioning dialog behind it.

   Split from talk.js on purpose — talk.js is the microphone and the socket and
   has no opinion about the page; this file is the page and has no opinion about
   audio. The live wall wires them together in two lines. */

(function (global) {
  "use strict";

  var MIC_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="2" width="6" height="11" rx="3"/>' +
    '<path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/></svg>';

  var state = { enabled: false, byId: {} };

  /* One read of the talk-back inventory, shared by every tile on the page. */
  function refresh() {
    return api("/api/v1/talkback/cameras").then(function (r) {
      state.enabled = !!r.enabled;
      state.byId = {};
      (r.cameras || []).forEach(function (c) { state.byId[c.camera_id] = c; });
      return state;
    }).catch(function () { return state; });
  }

  function edgeIdOrEmpty() {
    return (typeof global.cvEdgeId === "function") ? global.cvEdgeId() : Promise.resolve("");
  }

  /* Commissioning: the ONE moment the TP-Link account password is handled. It
     goes straight to the device, which stores only its hashes — so this dialog
     never has anything to prefill, and deliberately shows an empty field even
     for a camera that is already configured. */
  function commission(camera) {
    return new Promise(function (resolve) {
      document.querySelectorAll(".cv-dialog-scrim").forEach(function (d) { d.remove(); });
      var scrim = document.createElement("div");
      scrim.className = "cv-dialog-scrim";
      scrim.innerHTML =
        '<div class="cv-dialog" role="dialog" aria-modal="true">' +
        '<div class="cv-dialog-title">Enable talk-back</div>' +
        '<div class="cv-dialog-msg">' +
          'To speak through <b></b>, this device needs the <b>TP-Link account ' +
          'password</b> for the Tapo app this camera is paired to — not the ' +
          "camera's stream username and password. It is stored as a one-way " +
          'hash on this device and never sent anywhere else.' +
        '</div>' +
        '<div class="mt"><input type="password" autocomplete="off" ' +
          'placeholder="TP-Link account password" data-cv="pw" style="width:100%"></div>' +
        '<div class="cv-dialog-msg" data-cv="err" style="color:var(--bad)" hidden></div>' +
        '<div class="cv-dialog-actions">' +
          '<button class="btn btn-ghost" data-cv="cancel">Cancel</button>' +
          '<button class="btn btn-primary" data-cv="ok">Save &amp; test</button>' +
        '</div></div>';
      scrim.querySelector(".cv-dialog-msg b").textContent =
        prettyLabel(camera.camera_name || camera.camera_id);
      document.body.appendChild(scrim);

      var pw = scrim.querySelector('[data-cv="pw"]');
      var err = scrim.querySelector('[data-cv="err"]');
      var ok = scrim.querySelector('[data-cv="ok"]');
      pw.focus();

      function done(v) {
        scrim.remove();
        document.removeEventListener("keydown", onKey);
        resolve(v);
      }
      function fail(message) {
        err.textContent = message;
        err.hidden = false;
        ok.disabled = false;
        ok.textContent = "Save & test";
      }
      function save() {
        if (!pw.value) return fail("Enter the password.");
        ok.disabled = true;
        ok.textContent = "Testing…";
        err.hidden = true;
        edgeIdOrEmpty().then(function (edge) {
          return api("/api/v1/talkback/" + encodeURIComponent(camera.camera_id) +
                     "/credential", {
            method: "PUT",
            body: JSON.stringify({ edgeId: edge, password: pw.value }),
          }).then(function () {
            // Prove it against the real camera before telling anyone it works.
            return api("/api/v1/talkback/" + encodeURIComponent(camera.camera_id) +
                       "/test", { method: "POST", body: JSON.stringify({ edgeId: edge }) });
          });
        }).then(function () {
          toast("Talk-back enabled for " + prettyLabel(camera.camera_name), "ok");
          done(true);
        }).catch(function (e) {
          var d = (e && e.detail) || e || {};
          fail(d.message || d.detail || "The camera did not accept that password.");
        });
      }
      ok.onclick = save;
      scrim.querySelector('[data-cv="cancel"]').onclick = function () { done(false); };
      scrim.addEventListener("click", function (e) { if (e.target === scrim) done(false); });
      var onKey = function (e) {
        if (e.key === "Escape") { e.preventDefault(); done(false); }
        else if (e.key === "Enter") { e.preventDefault(); save(); }
      };
      document.addEventListener("keydown", onKey);
    });
  }

  /* Add the button to one tile. No-op when talk-back is switched off on the
     device, so a fleet that has not commissioned it sees no dead controls. */
  function mount(tile, camera) {
    if (!state.enabled || tile.querySelector(".talk-btn")) return null;

    var btn = document.createElement("button");
    btn.className = "talk-btn";
    btn.type = "button";
    btn.innerHTML = MIC_SVG + '<span class="talk-label">Hold to talk</span>';
    btn.title = "Hold to speak through this camera";
    btn.setAttribute("aria-label", "Hold to speak through " +
      prettyLabel(camera.camera_name || camera.camera_id));
    tile.appendChild(btn);

    var entry = state.byId[camera.camera_id] || {};
    var label = btn.querySelector(".talk-label");

    // Not commissioned yet: the button asks for the password instead of the
    // microphone, and only becomes a talk button once the camera has answered.
    if (!entry.configured) {
      btn.dataset.talk = "setup";
      label.textContent = "Enable talk";
      btn.onclick = function (e) {
        e.stopPropagation();
        commission(camera).then(function (okDone) {
          if (okDone) { refresh().then(function () { remount(tile, camera); }); }
        });
      };
      return null;
    }

    var handle = cvTalk.attach(btn, camera.camera_id, {
      onState: function (s, detail) {
        label.textContent =
          s === "live" ? "On air" :
          s === "connecting" ? "Connecting…" :
          s === "error" ? "Try again" : "Hold to talk";
        if (s === "error" && detail) toast(detail, "err", 5000);
      },
      onLevel: function (peak) {
        // A real level, not an animation: silence must look like silence.
        btn.style.setProperty("--talk-level", Math.min(1, peak * 3).toFixed(2));
      },
    });
    return handle;
  }

  function remount(tile, camera) {
    var old = tile.querySelector(".talk-btn");
    if (old) old.remove();
    mount(tile, camera);
  }

  global.cvTalkPanel = { refresh: refresh, mount: mount, state: state };
})(window);
