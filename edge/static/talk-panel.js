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

  var SPEAKER_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6 9H2v6h4l5 4V5Z"/>' +
    '<path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';

  // `error` is the difference between "this fleet has talk-back switched off"
  // (render nothing — a dead control is worse than no control) and "we could
  // not find out" (render something that SAYS so). Conflating the two is how a
  // missing button becomes a mystery instead of a message.
  var state = { enabled: false, byId: {}, error: "" };

  /* Every live listen control on the page, so that switching one room on can
     switch the rest off. Entries remove themselves in destroy(). */
  var listeners = [];

  /* One read of the talk-back inventory, shared by every tile on the page.

     `ensure` is what the live wall calls on every 5-second sync: this list only
     changes when someone commissions a camera, so it is fetched ONCE and the
     same promise handed back forever after. `refresh` forces a re-read, and is
     called exactly where that can matter — after commissioning. */
  var inflight = null;

  function ensure() {
    if (!inflight) { lastRead = Date.now(); inflight = refresh(); }
    else sync();
    return inflight;
  }

  function refresh() {
    // The inventory names the rooms in a home, so it is authenticated like
    // everything else here — which means resolving the edge id BEFORE asking.
    inflight = edgeIdOrEmpty().then(function (edge) {
      return api("/api/v1/talkback/cameras" +
                 (edge ? "?edge_id=" + encodeURIComponent(edge) : ""));
    }).then(function (r) {
      state.enabled = !!r.enabled;
      state.error = "";
      state.byId = {};
      (r.cameras || []).forEach(function (c) { state.byId[c.camera_id] = c; });
      return state;
    }).catch(function (e) {
      inflight = null;             // a failed read must not be cached forever
      state.enabled = false;
      var d = (e && e.detail) || {};
      state.error = d.message ||
        "Could not read the talk-back settings from this device. The live " +
        "video is unaffected; the microphone is unavailable until this answers.";
      return state;
    });
    return inflight;
  }

  function edgeIdOrEmpty() {
    return (typeof global.cvEdgeId === "function") ? global.cvEdgeId() : Promise.resolve("");
  }

  /* Commissioning: the ONE moment the TP-Link account password is handled. It
     goes straight to the device, which stores only its hashes — so this dialog
     never has anything to prefill, and deliberately shows an empty field even
     for a camera that is already configured. */
  /* THE setup for talk-back: one password for the whole home.

     Every camera in a home is paired to one TP-Link account, so this asks once
     and every camera uses it — including a camera added next year. It goes
     straight to the device, which stores only its hashes, so the field is never
     prefilled. After saving, the device checks every camera silently and the
     dialog shows what each one said, in place: a carer or technician sees
     "LOUNGE ready, LIVING ROOM refused" instead of discovering it on a press. */
  function commission() {
    return new Promise(function (resolve) {
      document.querySelectorAll(".cv-dialog-scrim").forEach(function (d) { d.remove(); });
      var scrim = document.createElement("div");
      scrim.className = "cv-dialog-scrim";
      scrim.innerHTML =
        '<div class="cv-dialog" role="dialog" aria-modal="true">' +
        '<div class="cv-dialog-title">Talk-back for this home</div>' +
        '<div class="cv-dialog-msg">' +
          'Enter the <b>TP-Link account password</b> — the one used to sign in to ' +
          'the Tapo app these cameras are paired to. Not the camera\'s stream ' +
          'password. You enter it <b>once</b>; every camera in this home uses it. ' +
          'It is stored as a one-way hash on this device and sent nowhere else.' +
        '</div>' +
        '<div class="mt"><input type="password" autocomplete="off" ' +
          'placeholder="TP-Link account password" data-cv="pw" style="width:100%"></div>' +
        '<div class="talk-verdicts mt" data-cv="verdicts" hidden></div>' +
        '<div class="cv-dialog-msg" data-cv="err" style="color:var(--bad)" hidden></div>' +
        '<div class="cv-dialog-actions">' +
          '<button class="btn btn-ghost" data-cv="cancel">Close</button>' +
          '<button class="btn btn-ghost" data-cv="recheck" hidden>Check again</button>' +
          '<button class="btn btn-primary" data-cv="ok">Save &amp; check cameras</button>' +
        '</div></div>';
      document.body.appendChild(scrim);

      var pw = scrim.querySelector('[data-cv="pw"]');
      var err = scrim.querySelector('[data-cv="err"]');
      var ok = scrim.querySelector('[data-cv="ok"]');
      var again = scrim.querySelector('[data-cv="recheck"]');
      var list = scrim.querySelector('[data-cv="verdicts"]');
      var changed = false;
      pw.focus();

      function done() {
        pw.value = "";              // the plaintext leaves with the dialog
        scrim.remove();
        document.removeEventListener("keydown", onKey);
        resolve(changed);
      }
      function fail(message) {
        err.textContent = message;
        err.hidden = false;
      }
      function busy(on, label) {
        ok.disabled = on; again.disabled = on;
        ok.textContent = on ? label : "Save & check cameras";
      }

      /* One line per camera, in the camera's own words. */
      function show(res) {
        var cams = (res && res.cameras) || {};
        list.innerHTML = "";
        Object.keys(cams).forEach(function (cid) {
          var v = cams[cid] || {};
          var row = document.createElement("div");
          row.className = "talk-verdict";
          row.dataset.state = v.state || "unknown";
          var name = (state.byId[cid] && state.byId[cid].camera_name) || cid;
          var b = document.createElement("b");
          b.textContent = prettyLabel(name);
          var t = document.createElement("span");
          t.textContent = v.state === "ready"
            ? " ready" + (v.elapsed_ms != null ? " (" + v.elapsed_ms + " ms)" : "")
            : " — " + (v.message || v.state);
          row.appendChild(b); row.appendChild(t);
          list.appendChild(row);
        });
        list.hidden = !Object.keys(cams).length;
        again.hidden = !!(res && res.all_ready);
        if (res && res.all_ready) toast("Talk-back is ready on every camera.", "ok");
      }

      function save() {
        if (!pw.value) return fail("Enter the password.");
        err.hidden = true;
        busy(true, "Checking cameras…");
        edgeIdOrEmpty().then(function (edge) {
          return api("/api/v1/talkback/credential", {
            method: "PUT",
            body: JSON.stringify({ edgeId: edge, password: pw.value }),
          });
        }).then(function (res) {
          changed = true;
          pw.value = "";
          show(res);
        }).catch(function (e) {
          var d = (e && e.detail) || e || {};
          fail(d.message || "Could not save the password on this device.");
        }).then(function () { busy(false); });
      }

      /* For right after re-pairing a camera in the Tapo app. */
      function recheck() {
        err.hidden = true;
        busy(true, "Checking cameras…");
        edgeIdOrEmpty().then(function (edge) {
          return api("/api/v1/talkback/check", {
            method: "POST", body: JSON.stringify({ edgeId: edge }),
          });
        }).then(function (res) {
          changed = true;
          show(res);
        }).catch(function (e) {
          var d = (e && e.detail) || e || {};
          fail(d.message || "The check did not complete.");
        }).then(function () { busy(false); });
      }

      ok.onclick = save;
      again.onclick = recheck;
      scrim.querySelector('[data-cv="cancel"]').onclick = done;
      scrim.addEventListener("click", function (e) { if (e.target === scrim) done(); });
      var onKey = function (e) {
        if (e.key === "Escape") { e.preventDefault(); done(); }
        else if (e.key === "Enter" && document.activeElement === pw) {
          e.preventDefault(); save();
        }
      };
      document.addEventListener("keydown", onKey);
    });
  }

  /* LISTEN — the other half of two-way audio, and much cheaper than the first.

     The camera's microphone is already in the WHEP stream the tile is playing
     (live-view.js asks for it with `audio: true`); listening is nothing more
     than unmuting the element. No second connection, no new protocol, no
     credential. It must start from a click because browsers refuse to unmute
     audio that no one asked for. */
  function mountListen(tile, camera, stream) {
    if (!stream || !stream.listen || tile.querySelector(".listen-btn")) return null;

    var btn = document.createElement("button");
    btn.className = "listen-btn";
    btn.type = "button";
    btn.dataset.listen = "off";
    btn.innerHTML = SPEAKER_SVG + '<span class="talk-label">Listen</span>';
    btn.title = "Hear this room";
    btn.setAttribute("aria-label", "Listen to " +
      prettyLabel(camera.camera_name || camera.camera_id));
    tile.appendChild(btn);

    // ADOPT REALITY, never assume "off". This button gets rebuilt — by
    // commissioning, by a tile refresh — while the stream underneath it keeps
    // playing. A rebuilt button that assumed silence was the bug: it started a
    // click out of phase with the room, so the press that should have switched
    // a room off only re-asserted that it was on.
    var on = stream.listening ? stream.listening() : false;
    var ducked = false;
    var label = btn.querySelector(".talk-label");

    // Re-assert rather than toggle. `stream.listen()` is idempotent, so calling
    // it with what we already want costs nothing and guarantees the button and
    // the audio path cannot drift apart between clicks.
    function apply() {
      var has = stream.listen(on && !ducked);
      var live = stream.listening ? stream.listening() : (on && has);
      var s = on ? (ducked ? "ducked" : (live ? "on" : "waiting")) : "off";
      btn.dataset.listen = s;
      label.textContent =
        s === "on" ? "Listening" :
        s === "ducked" ? "Muted while talking" :
        // Asked for, but no audio track has arrived yet. Usually the stream is
        // still negotiating rather than the camera being deaf, so stay armed:
        // the moment sound arrives, listening starts (audioArrived below). A
        // camera with no microphone simply never arrives, and the button keeps
        // saying so rather than lying that it is listening.
        s === "waiting" ? "Waiting for audio…" : "Listen";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      return has;
    }

    btn.onclick = function (e) {
      e.stopPropagation();
      // The question is never "what did I set last time", it is "is this room
      // audible right now" — the one answer a rebuild cannot invalidate.
      on = !(stream.listening ? stream.listening() : on);
      // ONE room at a time. Two rooms playing at once is not more monitoring,
      // it is a wall of noise where nobody can tell which room a sound came
      // from — and it is the other way a carer ends up unable to switch a room
      // off: the one they muted was never the one they could hear.
      if (on) listeners.forEach(function (l) { if (l !== handle) l.stop(); });
      // ALWAYS applied. Turning listening OFF is an action in its own right —
      // guarding this behind `on` is how the button became one-way.
      apply();
    };

    var handle = {
      /* The stream finally produced an audio track. If the carer already asked
         to listen, honour it now — the click does not have to be repeated. */
      audioArrived: function () { if (on) apply(); },
      /* Half-duplex on purpose: a speaker and a microphone in the same room,
         both live, is a feedback loop. The camera runs its own echo
         cancellation, but ducking the carer's side too is what keeps a real
         room from howling — and it is what every intercom does. */
      duck: function (talking) {
        if (ducked === !!talking) return;
        ducked = !!talking;
        if (on) apply();
      },
      stop: function () { on = false; ducked = false; apply(); },
      /* The BUTTON is being replaced but the room is still on screen. Leave the
         audio exactly as the carer left it and just leave the roster — the
         replacement adopts the live state on mount. Silencing a room here is
         how commissioning one camera would mute another one mid-listen. */
      detach: function () {
        var i = listeners.indexOf(handle);
        if (i >= 0) listeners.splice(i, 1);
      },
      /* The TILE is going. Stop the audio as well — there will be nothing left
         to turn it off with. */
      destroy: function () {
        on = false; ducked = false;
        try { apply(); } catch (e) {}
        handle.detach();
      },
    };
    listeners.push(handle);
    // The stream may already have been playing before this button existed.
    apply();
    return handle;
  }

  /* Add the tile's audio controls. Listening needs nothing commissioned, so it
     appears whenever the stream carries sound; talking appears only when
     talk-back is switched on for the device, and a fleet that has not
     commissioned it sees no dead controls. */
  function mount(tile, camera, stream) {
    var listener = mountListen(tile, camera, stream);
    if (listener) tile.cvListen = listener;
    if (tile.querySelector(".talk-btn")) return null;

    // We could not find out whether talking is possible. Say that, on the tile,
    // instead of leaving a carer to wonder where the microphone went.
    if (state.error) {
      var warn = document.createElement("button");
      warn.className = "talk-btn";
      warn.type = "button";
      warn.dataset.talk = "error";
      warn.innerHTML = MIC_SVG + '<span class="talk-label">Talk unavailable</span>';
      warn.title = state.error;
      warn.onclick = function (e) {
        e.stopPropagation();
        toast(state.error, "err", 6000);
        // Ask again on demand: whatever was wrong may have been momentary.
        refresh().then(function () { remount(tile, camera, stream); });
      };
      tile.appendChild(warn);
      return null;
    }

    if (!state.enabled) return null;

    var btn = document.createElement("button");
    btn.className = "talk-btn";
    btn.type = "button";
    btn.innerHTML = MIC_SVG + '<span class="talk-label">Hold to talk</span>';
    btn.title = "Hold to speak through this camera";
    btn.setAttribute("aria-label", "Hold to speak through " +
      prettyLabel(camera.camera_name || camera.camera_id));
    tile.appendChild(btn);

    var entry = state.byId[camera.camera_id] || {};
    var ready = (entry.readiness || {}).state || "unknown";
    var label = btn.querySelector(".talk-label");
    remember(tile, camera, stream, entry);

    // The device checks every camera silently from boot, so by the time a carer
    // looks, it already knows whether a press would work. A camera it knows
    // WON'T work gets a button that fixes the cause, not a microphone that
    // fails after the carer has started speaking.
    if (!entry.configured || ready === "needs_password" || ready === "rejected") {
      btn.dataset.talk = "setup";
      label.textContent = ready === "rejected" ? "Talk: password refused"
                                               : "Set up talk";
      btn.title = (entry.readiness && entry.readiness.message) ||
        "Talk-back needs this home's TP-Link account password.";
      btn.onclick = function (e) {
        e.stopPropagation();
        commission().then(function (changed) {
          if (changed) refresh().then(remountAll);
        });
      };
      return null;
    }
    if (ready === "unsupported") {
      // A control that can never work is worse than no control.
      btn.remove();
      return null;
    }

    var handle = cvTalk.attach(btn, camera.camera_id, {
      onState: function (s, detail) {
        // "ready" is the held channel: the camera is ours and the next press
        // makes no one wait. Worth its own word on the button.
        label.textContent =
          s === "live" ? "On air" :
          s === "ready" ? "Talk" :
          s === "connecting" ? "Connecting…" :
          // The channel dropped and is coming back by itself. Named, because a
          // carer who is told what is happening waits; a carer shown nothing
          // presses again, and a second press is a second session.
          s === "reconnecting" ? "Reconnecting…" :
          s === "error" ? "Try again" :
          ready === "unreachable" ? "Speaker offline" : "Hold to talk";
        if (s === "idle" && ready === "unreachable")
          btn.title = entry.readiness.message || btn.title;
        if (s === "error" && detail) toast(detail, "err", 5000);
      },
      onLevel: function (peak) {
        // A real level, not an animation: silence must look like silence.
        btn.style.setProperty("--talk-level", Math.min(1, peak * 3).toFixed(2));
      },
      onTalking: function (talking) {
        if (listener) listener.duck(talking);
      },
      // Refused for a reason that will not change by pressing again (wrong
      // password, no credential). The device already recorded it; re-read the
      // inventory so THIS tile turns into the setup button for the next press.
      onRefused: function () {
        refresh().then(function () { remountIfIdle(tile); });
      },
    });
    tile.cvTalk = handle;
    return handle;
  }

  /* Tear both controls down with their tile.

     Not housekeeping: the talk handle owns a WebSocket that may still be
     HOLDING the camera's speaker, plus window/document listeners that would
     outlive the button. A tile removed mid-hold would otherwise sit on a
     household's speaker until the hold window expired. */
  function unmount(tile) {
    forget(tile);
    if (tile.cvTalk) { tile.cvTalk.destroy(); tile.cvTalk = null; }
    if (tile.cvListen) { tile.cvListen.destroy(); tile.cvListen = null; }
  }

  /* The tile's stream produced an audio track. */
  function noteAudio(tile) {
    if (tile && tile.cvListen) tile.cvListen.audioArrived();
  }

  /* Every tile this page mounted, so a home-wide change (one password for every
     camera) can rebuild every tile, and so a periodic re-read can rebuild just
     the tiles whose readiness actually changed. */
  var mounted = [];

  function signature(entry) {
    return [entry.configured ? 1 : 0, (entry.readiness || {}).state || ""].join("|");
  }
  function remember(tile, camera, stream, entry) {
    var row = mounted.filter(function (m) { return m.tile === tile; })[0];
    if (!row) { row = { tile: tile }; mounted.push(row); }
    row.camera = camera; row.stream = stream; row.sig = signature(entry);
  }
  function forget(tile) {
    mounted = mounted.filter(function (m) { return m.tile !== tile; });
  }
  /* Never rebuild under a live conversation: remounting destroys the talk
     handle, and the talk handle may be holding the camera's speaker. */
  function remountIfIdle(tile) {
    var row = mounted.filter(function (m) { return m.tile === tile; })[0];
    if (!row || !tile.isConnected) return;
    if (tile.cvTalk && tile.cvTalk.isHeld && tile.cvTalk.isHeld()) return;
    remount(tile, row.camera, row.stream);
  }
  function remountAll() {
    mounted.slice().forEach(function (m) { remountIfIdle(m.tile); });
  }

  /* The device re-checks cameras on its own (a camera re-paired in the Tapo app
     turns ready within the hour, or at once on "Check again"). Re-read the
     inventory every READINESS_MS, and rebuild only the tiles whose state moved. */
  var READINESS_MS = 30000;
  var lastRead = 0;
  function sync() {
    if (Date.now() - lastRead < READINESS_MS) return;
    lastRead = Date.now();
    refresh().then(function () {
      mounted.slice().forEach(function (m) {
        var entry = state.byId[m.camera.camera_id];
        if (entry && signature(entry) !== m.sig) remountIfIdle(m.tile);
      });
    });
  }

  /* Rebuild BOTH controls after commissioning. The listen button goes too,
     even though nothing about it changed: the talk handle needs a live
     reference to it for ducking, and a stale one would leave a carer listening
     to their own voice coming back out of the room. */
  function remount(tile, camera, stream) {
    // Release the handles BEFORE the buttons go — dropping a button releases
    // neither the roster entry nor a held camera speaker. The TALK handle is
    // destroyed, because it owns a socket that may be holding a household's
    // speaker. The LISTEN handle is only detached: the room is still on screen
    // and the carer may still be listening to it, so the replacement button
    // adopts that rather than silencing it.
    if (tile.cvTalk) { tile.cvTalk.destroy(); tile.cvTalk = null; }
    if (tile.cvListen) { tile.cvListen.detach(); tile.cvListen = null; }
    tile.querySelectorAll(".talk-btn, .listen-btn").forEach(function (b) {
      b.remove();
    });
    return mount(tile, camera, stream);
  }

  // `remount` is exported for one reason: it is the path that rebuilds a
  // control over a room that may still be audible, which is exactly where the
  // listen toggle went out of step before. A path that subtle should be
  // reachable from a test, not only from a commissioning dialog.
  global.cvTalkPanel = { refresh: refresh, ensure: ensure, mount: mount,
                         unmount: unmount, remount: remount,
                         // Camera setup opens the same one-password dialog.
                         commission: commission,
                         noteAudio: noteAudio, state: state };
})(window);
