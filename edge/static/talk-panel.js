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

  /* One read of the talk-back inventory, shared by every tile on the page.

     `ensure` is what the live wall calls on every 5-second sync; it re-reads at
     most every REFRESH_MS. The read is cheap (the edge answers from memory and
     touches no camera) and carries each camera's line state and who holds its
     floor, so a tile says "Nurse Priya is talking" before anyone presses.
     `refresh` forces a re-read — after commissioning, where it matters. */
  var REFRESH_MS = 15000;
  var inflight = null;
  var readAt = 0;

  function ensure() {
    if (!inflight || Date.now() - readAt > REFRESH_MS) inflight = refresh();
    return inflight;
  }

  function refresh() {
    // The inventory names the rooms in a home, so it is authenticated like
    // everything else here — which means resolving the edge id BEFORE asking.
    inflight = edgeIdOrEmpty().then(function (edge) {
      return api("/api/v1/talkback/cameras" +
                 (edge ? "?edge_id=" + encodeURIComponent(edge) : ""));
    }).then(function (r) {
      readAt = Date.now();
      state.enabled = !!r.enabled;
      state.error = "";
      state.byId = {};
      (r.cameras || []).forEach(function (c) { state.byId[c.camera_id] = c; });
      applyInventory();
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
  /* `camera` given = the per-camera override: for the rare camera paired to a
     DIFFERENT TP-Link account than the rest of the home. Same dialog, same
     verdicts, scoped to that one camera. */
  function commission(camera) {
    var only = camera && camera.camera_id ? camera : null;
    var onlyName = only ? prettyLabel(only.camera_name || only.camera_id) : "";
    return new Promise(function (resolve) {
      document.querySelectorAll(".cv-dialog-scrim").forEach(function (d) { d.remove(); });
      var scrim = document.createElement("div");
      scrim.className = "cv-dialog-scrim";
      scrim.innerHTML =
        '<div class="cv-dialog" role="dialog" aria-modal="true">' +
        '<div class="cv-dialog-title" data-cv="title">Talk-back for this home</div>' +
        '<div class="cv-dialog-msg" data-cv="only" hidden>' +
          'A password for <b data-cv="only-name"></b> only. Use this when that ' +
          'camera is paired to a <b>different TP-Link account</b> from the rest ' +
          'of the home. It takes priority over the home password for this camera.' +
        '</div>' +
        '<div class="cv-dialog-msg" data-cv="home">' +
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
      if (only) {
        scrim.querySelector('[data-cv="title"]').textContent = "Talk-back for " + onlyName;
        scrim.querySelector('[data-cv="only-name"]').textContent = onlyName;
        scrim.querySelector('[data-cv="only"]').hidden = false;
        scrim.querySelector('[data-cv="home"]').hidden = true;
      }

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
        if (only) {
          var one = {};
          if (cams[only.camera_id]) one[only.camera_id] = cams[only.camera_id];
          cams = one;
          res = { cameras: one, all_ready: !!(one[only.camera_id] &&
                                              one[only.camera_id].state === "ready") };
        }
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
        if (res && res.all_ready)
          toast(only ? "Talk-back is ready on " + onlyName + "."
                     : "Talk-back is ready on every camera.", "ok");
      }

      function save() {
        if (!pw.value) return fail("Enter the password.");
        err.hidden = true;
        busy(true, "Checking cameras…");
        edgeIdOrEmpty().then(function (edge) {
          var body = JSON.stringify({ edgeId: edge, password: pw.value });
          if (!only)
            return api("/api/v1/talkback/credential", { method: "PUT", body: body });
          // One camera: store its override, then have the device check it.
          return api("/api/v1/talkback/" + encodeURIComponent(only.camera_id) +
                     "/credential", { method: "PUT", body: body })
            .then(function () {
              return api("/api/v1/talkback/check", { method: "POST",
                body: JSON.stringify({ edgeId: edge }) });
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
    var label = btn.querySelector(".talk-label");
    var meter = null;

    /* While listening, the ring shows the room's real level — including while
       the carer is talking (full duplex), which is how a carer can SEE the room
       is still coming in. */
    function meterOn(run) {
      if (run && !meter && stream.audioLevel) {
        meter = setInterval(function () {
          stream.audioLevel().then(function (l) {
            btn.style.setProperty("--listen-level", Math.min(1, l * 4).toFixed(2));
          });
        }, 250);
      } else if (!run && meter) {
        clearInterval(meter);
        meter = null;
        btn.style.removeProperty("--listen-level");
      }
    }

    // Re-assert rather than toggle. `stream.listen()` is idempotent, so calling
    // it with what we already want costs nothing and guarantees the button and
    // the audio path cannot drift apart between clicks.
    function apply() {
      var has = stream.listen(on);
      var live = stream.listening ? stream.listening() : (on && has);
      var s = on ? (live ? "on" : "waiting") : "off";
      btn.dataset.listen = s;
      label.textContent =
        s === "on" ? "Listening" :
        // Asked for, but no audio track has arrived yet. Usually the stream is
        // still negotiating rather than the camera being deaf, so stay armed:
        // the moment sound arrives, listening starts (audioArrived below). A
        // camera with no microphone simply never arrives, and the button keeps
        // saying so rather than lying that it is listening.
        s === "waiting" ? "Waiting for audio…" : "Listen";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      meterOn(s === "on");
      return has;
    }

    btn.onclick = function (e) {
      e.stopPropagation();
      // The question is never "what did I set last time", it is "is this room
      // audible right now" — the one answer a rebuild cannot invalidate.
      on = !(stream.listening ? stream.listening() : on);
      // ALWAYS applied. Turning listening OFF is an action in its own right —
      // guarding this behind `on` is how the button became one-way.
      apply();
    };

    /* EVERY ROOM IS ITS OWN TOGGLE. Any number can be listened to at once —
       the microphone is already in each tile's stream, so a second room costs
       no connection and takes nothing from the first. Rooms were once
       exclusive to stop a carer losing track of which one they could hear;
       what actually fixes that is telling them, so each button reads the live
       audio path (never a remembered flag) and its ring shows THAT room's
       level. The room a sound came from is the tile the ring moved on. */

    var handle = {
      /* The stream finally produced an audio track. If the carer already asked
         to listen, honour it now — the click does not have to be repeated. */
      audioArrived: function () { if (on) apply(); },
      /* FULL DUPLEX: every room a carer is listening to stays audible while
         they talk, so a resident who answers mid-sentence is heard. Echo is
         cancelled at both ends instead of by muting — the camera runs its own
         echo cancellation (talkback_mode "aec"), and the carer's microphone
         asks the browser for it (talk.js), which cancels everything the page
         is playing, not just the room being spoken to. Headphones remove any
         echo that is left. */
      /* The BUTTON is being replaced but the room is still on screen. Leave the
         audio exactly as the carer left it — the replacement adopts the live
         state on mount. Silencing a room here is how commissioning one camera
         would mute another one mid-listen. */
      detach: function () { meterOn(false); },
      /* The TILE is going. Stop the audio as well — there will be nothing left
         to turn it off with. */
      destroy: function () {
        on = false;
        try { apply(); } catch (e) {}
        meterOn(false);
      },
    };
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
    if (!entry.configured || ready === "needs_password" || ready === "rejected" ||
        ready === "paused") {
      btn.dataset.talk = "setup";
      label.textContent = ready === "rejected" ? "Talk: password refused" :
                          ready === "paused" ? "Talk paused" : "Set up talk";
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

    var speaker = "";           // who holds this camera's floor, if not us
    var handle = cvTalk.attach(btn, camera.camera_id, {
      onState: function (s, detail) {
        label.textContent =
          s === "live" ? "On air" :
          s === "connecting" ? "Connecting…" :
          // The connection dropped mid-sentence and is coming back by itself.
          // Named, because a carer who is told what is happening waits.
          s === "reconnecting" ? "Reconnecting…" :
          s === "busy" ? (speaker || "Someone") + " is talking" :
          s === "error" ? "Try again" :
          ready === "unreachable" ? "Speaker offline" :
          ready === "in_use" ? "Speaker in use" : "Hold to talk";
        if (s === "idle" && (ready === "unreachable" || ready === "in_use"))
          btn.title = entry.readiness.message || btn.title;
        if (s === "error" && detail) toast(detail, "err", 5000);
      },
      // Who holds the floor, from the inventory — so the button says "Nurse
      // Priya is talking" BEFORE anyone presses into a refusal.
      onFloor: function (state, by, mine) {
        speaker = state !== "free" && !mine ? by : "";
      },
      onLevel: function (peak) {
        // A real level, not an animation: silence must look like silence.
        btn.style.setProperty("--talk-level", Math.min(1, peak * 3).toFixed(2));
      },
      // Refused for a reason that will not change by pressing again (wrong
      // password, no credential). The device already recorded it; re-read the
      // inventory so THIS tile turns into the setup button for the next press.
      onRefused: function () {
        refresh().then(function () { remountIfIdle(tile); });
      },
    });
    tile.cvTalk = handle;
    handle.showFloor(entry.floor);
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

  /* A fresh inventory: rebuild only the tiles whose camera actually changed
     (re-paired in the Tapo app, a line that dropped), and tell every other tile
     who holds its floor now. */
  function applyInventory() {
    mounted.slice().forEach(function (m) {
      var entry = state.byId[m.camera.camera_id];
      if (!entry) return;
      if (signature(entry) !== m.sig) remountIfIdle(m.tile);
      else if (m.tile.cvTalk) m.tile.cvTalk.showFloor(entry.floor);
    });
  }

  /* HOLD SPACE TO TALK: the keyboard's press-and-hold, into whichever tile the
     page calls selected. It drives the SAME talk handle the button does — key
     down is the press, key up is the release — so every rule (one microphone,
     the floor, refusals) is the button's, not a second copy of them.

     Stays out of the way: ignored while typing in a field, with a modifier held
     (a browser shortcut), while a dialog is open, and for the key's
     auto-repeat. The default is prevented so Space neither scrolls the page nor
     "clicks" a focused button (a focused Listen would otherwise toggle). The
     turn ends on key up, on the window losing focus, and on the tab hiding —
     a stuck key must never leave a live microphone in a room. */
  function holdSpaceToTalk(selectedTile) {
    var held = null;                        // the talk handle Space is holding
    function ours(e) {
      var t = e.target;
      return (e.code === "Space" || e.key === " ") && !e.ctrlKey && !e.altKey &&
        !e.metaKey && !(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) &&
        !document.querySelector(".cv-dialog-scrim");
    }
    function letGo() {
      if (held) { held.release(); held = null; }
    }
    document.addEventListener("keydown", function (e) {
      if (!ours(e)) return;
      e.preventDefault();
      if (e.repeat || held) return;
      var tile = selectedTile();
      if (!tile) {
        toast("Click a camera first, then hold Space to talk to it.", "", 3200);
        return;
      }
      if (!tile.cvTalk) {
        toast("Talk-back is not ready on this camera — see its Talk button.", "", 3200);
        return;
      }
      held = tile.cvTalk;
      held.press();
    });
    document.addEventListener("keyup", function (e) {
      if (!ours(e)) return;
      e.preventDefault();
      letGo();
    });
    global.addEventListener("blur", letGo);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) letGo();
    });
  }

  /* Rebuild BOTH controls after commissioning, so the tile reads as one fresh
     unit. The listen button is only detached, never silenced (see below). */
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
                         holdSpaceToTalk: holdSpaceToTalk,
                         // Camera setup opens the same one-password dialog.
                         commission: commission,
                         noteAudio: noteAudio, state: state };
})(window);
