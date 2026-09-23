/* Push-to-talk: the carer's microphone -> the camera's speaker.

   One WebSocket per camera — /api/v1/talkback/{camera_id}/stream — opened on the
   first press. Connecting claims that camera's FLOOR; {"type":"open"} is the
   grant. Speech flows for as long as the button is held; releasing lets the
   floor go (the edge keeps it a few seconds so the carer can answer), and the
   socket stays open a while so the next press is instant — without holding the
   room. The camera side is the edge's own always-open line, so no press waits
   for a camera handshake. See edge/api/talkback_routes.py.

   Deliberately press-and-hold. A toggle leaves hot microphones open in living
   rooms; a held button cannot. Releasing it, hiding the tab or letting the
   pointer slip all end the turn.

     mic -> AudioWorklet (resample + A-law, talk-worklet.js) -> this camera's
     socket -> the edge -> the camera's line

   The microphone and its audio graph stay WARM for a few seconds after a turn,
   because re-acquiring them costs ~300 ms; the worklet is muted between turns,
   so warm never means live. */

(function (global) {
  "use strict";

  var PREFIX = global.CERAVIS_PREFIX ||
    ((location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/) || [])[1] || "");
  var WARM_MS = 8000;             // how long the mic stays open after a turn
  var PING_MS = 20000;            // keeps proxies from idling an open socket
  var CONNECT_TIMEOUT_MS = 9000;  // a handshake that never answers must not hang
  var RETRY_MIN_MS = 300;         // a dropped connection mid-sentence: 0.3 s ... 3 s,
  var RETRY_MAX_MS = 3000;        // for at most 15 s, then say so
  var REJOIN_GIVE_UP_MS = 15000;
  /* Speech that cannot be sent right now (the socket is briefly gone, or slow) is
     already late. Keep only the newest 100 ms of it: the OLDEST frames are the
     ones to lose, because the newest are the words still being said. */
  var OUTBOX_MAX_FRAMES = 5;
  var SOCKET_MAX_BUFFERED = 960;  // ~120 ms of A-law waiting in the socket
  /* Refusals whose fix is the camera's password, not pressing again. */
  var PASSWORD_CODES = { unauthorized: 1, no_credential: 1, cooldown: 1 };
  /* This page's id. Sent on every connect, so a reconnect gets its floor back. */
  var CLIENT_ID = "pg-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);

  var edgePromise = null;
  function edgeId() {
    if (typeof global.cvEdgeId === "function") return global.cvEdgeId();
    if (PREFIX) return Promise.resolve(PREFIX.slice(1));
    if (!edgePromise) {
      edgePromise = fetch("/api/v1/account")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (a) { return (a && (a.edge_id || (a.user && a.user.edgeId))) || ""; })
        .catch(function () { return ""; });
    }
    return edgePromise;
  }

  /* Who is talking, for other carers and the edge's talk log. An app that
     embeds this sets CV_TALK_NAME (and CV_TALK_USER_ID) from its signed-in user. */
  function identity() {
    return "&name=" + encodeURIComponent(global.CV_TALK_NAME || "CERAVIS live wall") +
      (global.CV_TALK_USER_ID ? "&user_id=" + encodeURIComponent(global.CV_TALK_USER_ID) : "");
  }

  /* Why a turn could not start, in words a carer can act on. */
  function micError(err) {
    var name = (err && err.name) || "";
    if (name === "NotAllowedError" || name === "SecurityError")
      return "Microphone blocked. Allow microphone access for this site in your " +
             "browser's address-bar permissions, then try again.";
    if (name === "NotFoundError" || name === "OverconstrainedError")
      return "No microphone found on this device.";
    if (name === "NotReadableError")
      return "The microphone is in use by another app.";
    return "Could not open the microphone" + (name ? " (" + name + ")" : "") + ".";
  }

  /* Browsers refuse getUserMedia outside a secure context: talk-back works on
     the https:// fleet address and on localhost, never on http://<device-ip>. */
  function contextError() {
    if (!global.isSecureContext)
      return "Talk-back needs a secure (https) page. Open the CERAVIS address " +
             "your device was given (https://…), not the http://" +
             location.hostname + " one.";
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
      return "This browser cannot capture a microphone.";
    if (!global.AudioWorkletNode)
      return "This browser is too old for talk-back (no AudioWorklet).";
    if (!global.WebSocket) return "This browser cannot open a WebSocket.";
    return "";
  }

  /* ---- the microphone: one per page, owned by one button at a time ------- */

  var graph = null;              // { ctx, stream, node, source }
  var warmTimer = null;
  var gain = 0;                  // the device's mic gain, from its "open" message
  /* The button the microphone is speaking for right now. A page has one
     microphone and a live wall has a talk button per camera; wiring it to each
     button sent speech to the wrong room and let one button mute another. */
  var owner = null;

  function route(ev) {
    var d = ev.data;
    if (d && d.type === "audio" && owner) owner.frame(d);
  }

  function releaseGraph() {
    if (!graph) return;
    try { graph.node.disconnect(); } catch (e) {}
    try { graph.source.disconnect(); } catch (e) {}
    try { graph.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    try { graph.ctx.close(); } catch (e) {}
    graph = null;
  }

  function acquireGraph() {
    if (warmTimer) { clearTimeout(warmTimer); warmTimer = null; }
    if (graph) {
      // A warm graph is not necessarily a RUNNING one: browsers suspend an
      // AudioContext when a tab is backgrounded. Resume on every acquire.
      if (graph.ctx.state !== "running")
        return graph.ctx.resume().catch(function () {}).then(function () { return graph; });
      return Promise.resolve(graph);
    }
    return navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Echo cancellation matters doubly because listening stays on while
        // talking: the room's voice plays from this device's speaker while its
        // microphone is live. The camera cancels its own echo on its side.
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      },
      video: false,
    }).then(function (stream) {
      var ctx;
      try { ctx = new (global.AudioContext || global.webkitAudioContext)({ sampleRate: 8000 }); }
      catch (e) { ctx = new (global.AudioContext || global.webkitAudioContext)(); }
      return ctx.audioWorklet.addModule("talk-worklet.js").then(function () {
        var source = ctx.createMediaStreamSource(stream);
        var node = new AudioWorkletNode(ctx, "talk-processor");
        source.connect(node);
        // A worklet with no destination is stopped by some browsers; a
        // zero-gain sink keeps it scheduled without playing anything back.
        var sink = ctx.createGain();
        sink.gain.value = 0;
        node.connect(sink).connect(ctx.destination);
        node.port.onmessage = route;     // wired ONCE; `owner` decides who hears it
        if (gain) node.port.postMessage({ type: "gain", value: gain });
        graph = { ctx: ctx, stream: stream, node: node, source: source };
        return ctx.resume().catch(function () {}).then(function () { return graph; });
      });
    });
  }

  function mute(on) {
    if (graph) graph.node.port.postMessage({ type: "mute", value: !!on });
  }

  function keepWarm() {
    if (warmTimer) clearTimeout(warmTimer);
    warmTimer = setTimeout(releaseGraph, WARM_MS);
  }

  /* ---- one camera's talk button, and its socket -------------------------- */

  function attach(button, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var ws = null, open = false, connecting = false;
    var pressed = false;         // the button is physically down
    var state = "";
    var sentence = "", refusal = "";   // the edge's last {"type":"error"}
    var outbox = [];
    var holdMs = 58000, idleTimer = null, pingTimer = null;
    var rejoining = false, rejoinUntil = 0, rejoinDelay = RETRY_MIN_MS, rejoinTimer = null;

    function setState(s, detail) {
      state = s;
      button.dataset.talk = s;
      onState(s, detail || "");
    }

    function sendJson(obj) {
      if (ws && ws.readyState === 1 && open) ws.send(JSON.stringify(obj));
    }

    function flush() {
      while (outbox.length && ws && ws.readyState === 1 && open &&
             ws.bufferedAmount < SOCKET_MAX_BUFFERED) ws.send(outbox.shift());
      while (outbox.length > OUTBOX_MAX_FRAMES) outbox.shift();
    }

    function speak() { mute(false); setState("live"); flush(); }
    function quiet() { if (owner === me) mute(true); }

    /* An open socket holds nothing, but it is not kept forever: closed a
       little before the edge would close it (its hold_secs). */
    function idleSoon() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () { if (!pressed) closeSocket(); }, holdMs);
    }

    function closeSocket() {
      clearInterval(pingTimer); clearTimeout(idleTimer);
      var s = ws;
      ws = null; open = false;
      if (s) { try { if (s.readyState === 1) s.send(JSON.stringify({ type: "stop" })); s.close(); } catch (e) {} }
    }

    var me = {
      camera: cameraId,
      frame: function (d) {
        if (opts.onLevel) opts.onLevel(pressed ? (d.peak || 0) : 0);
        if (!pressed) return;
        outbox.push(d.frame);
        if (open) flush();
        else while (outbox.length > OUTBOX_MAX_FRAMES) outbox.shift();
      },
      hangUp: function () { if (pressed) release(); },   // another button took the mic
    };

    function connect() {
      if (ws || connecting) return;
      connecting = true;
      edgeId().then(function (edge) {
        connecting = false;
        if (ws || (!pressed && !rejoining)) return;       // let go before we got here
        var scheme = location.protocol === "https:" ? "wss://" : "ws://";
        var sock = new WebSocket(scheme + location.host + PREFIX + "/api/v1/talkback/" +
          encodeURIComponent(cameraId) + "/stream?client_id=" + encodeURIComponent(CLIENT_ID) +
          (edge ? "&edge_id=" + encodeURIComponent(edge) : "") + identity());
        sock.binaryType = "arraybuffer";
        ws = sock;
        var timer = setTimeout(function () { if (!open && ws === sock) sock.close(); },
                               CONNECT_TIMEOUT_MS);

        sock.onmessage = function (ev) {
          var m;
          try { m = JSON.parse(ev.data); } catch (e) { return; }
          if (m.type === "open") {
            clearTimeout(timer);
            open = true;
            rejoining = false; rejoinUntil = 0; rejoinDelay = RETRY_MIN_MS;
            if (m.mic_gain) {
              gain = m.mic_gain;
              if (graph) graph.node.port.postMessage({ type: "gain", value: gain });
            }
            if (m.hold_secs > 2) holdMs = (m.hold_secs - 2) * 1000;
            clearInterval(pingTimer);
            pingTimer = setInterval(function () { sendJson({ type: "ping" }); }, PING_MS);
            if (!pressed) {                    // let go before the grant came
              sendJson({ type: "release" });
              setState("idle");
              idleSoon();
            } else if (graph) speak();         // else the microphone speaks when ready
          } else if (m.type === "error") {
            sentence = m.message || "";
            refusal = m.code || "";
          } else if (m.type === "stats") {
            if (opts.onHealth) opts.onHealth(m);
          }
        };

        sock.onclose = function (ev) {
          clearTimeout(timer);
          if (ws !== sock) return;             // we closed it ourselves
          ws = null; open = false;
          clearInterval(pingTimer);
          var code = refusal || (/^([a-z_]+):/.exec(ev.reason || "") || [])[1] || "";
          var why = sentence || (ev.reason || "").replace(/^[a-z_]+:\s*/, "");
          sentence = ""; refusal = "";
          if (code) {
            // The edge refused and said why. Pressing again will not change a
            // password or a busy room this instant: say it, do not retry.
            pressed = false; rejoining = false; outbox.length = 0; quiet();
            setState("error", why || "The camera's speaker is not available.");
            if (PASSWORD_CODES[code] && opts.onRefused) opts.onRefused(code);
            return;
          }
          if (!pressed) { rejoining = false; setState("idle"); return; }
          if (ev.code === 1000 || ev.code === 1001) { connect(); return; }   // idle close under a press
          if (rejoin()) return;                // the network, mid-sentence
          pressed = false; outbox.length = 0; quiet();
          setState("error", "Lost the connection to the device. Press again to retry.");
        };
        sock.onerror = function () { /* onclose carries the outcome */ };
      });
    }

    function rejoin() {
      var now = Date.now();
      if (!rejoinUntil) rejoinUntil = now + REJOIN_GIVE_UP_MS;
      if (now >= rejoinUntil) { rejoining = false; rejoinUntil = 0; return false; }
      rejoining = true;
      setState("reconnecting");                // keeps capturing: the newest 100 ms
      rejoinTimer = setTimeout(function () { if (rejoining && !ws) connect(); }, rejoinDelay);
      rejoinDelay = Math.min(Math.round(rejoinDelay * 1.8), RETRY_MAX_MS);
      return true;
    }

    function press() {
      if (pressed) return;
      var problem = contextError();
      if (problem) { setState("error", problem); return; }
      if (owner && owner !== me) owner.hangUp();  // one microphone, one room
      owner = me;
      pressed = true;
      outbox.length = 0;
      clearTimeout(idleTimer);
      setState("connecting");
      acquireGraph().then(function () {
        if (pressed && open) speak();
      }).catch(function (err) {
        if (!pressed) return;
        pressed = false;
        sendJson({ type: "release" });
        setState("error", micError(err));
      });
      if (!open) connect();
    }

    function release() {
      if (!pressed) return;
      pressed = false;
      quiet();
      outbox.length = 0;
      if (owner === me) keepWarm();
      if (rejoining) {
        rejoining = false; rejoinUntil = 0; clearTimeout(rejoinTimer);
        closeSocket();
      } else {
        sendJson({ type: "release" });
        idleSoon();
      }
      setState("idle");
    }

    /* Who holds this camera's floor, from the inventory (GET /cameras): so the
       button says "Nurse Priya is talking" before anyone presses into a refusal. */
    function showFloor(f) {
      f = f || { state: "free" };
      var mine = f.client_id === CLIENT_ID;
      if (opts.onFloor) opts.onFloor(f.state || "free", f.by || "", mine);
      if (pressed || state === "connecting" || state === "live" || state === "reconnecting") return;
      setState(f.state && f.state !== "free" && !mine ? "busy" : "idle");
    }

    // Press and hold. `setPointerCapture` keeps the release ours even if the
    // finger slides off the button mid-sentence.
    button.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      try { button.setPointerCapture(e.pointerId); } catch (err) {}
      press();
    });
    ["pointerup", "pointercancel"].forEach(function (name) {
      button.addEventListener(name, function (e) { e.stopPropagation(); release(); });
    });
    button.addEventListener("click", function (e) { e.stopPropagation(); });

    // A hidden tab or a lost window ends the turn — a hot mic must not survive a
    // tab switch.
    function onHidden() { if (document.hidden) release(); }
    global.addEventListener("blur", release);
    document.addEventListener("visibilitychange", onHidden);

    setState("idle");

    return {
      /* The same press the button makes — for a keyboard shortcut (talk-panel's
         holdSpaceToTalk). Every rule is in press(): secure page, one microphone
         per page, the socket, the grant. */
      press: press,
      release: release,
      stop: release,
      showFloor: showFloor,
      isLive: function () { return pressed && open; },
      isHeld: function () { return pressed || rejoining; },
      /* The tile is going: end any turn, close the socket, stop listening. */
      destroy: function () {
        release();
        closeSocket();
        clearTimeout(rejoinTimer);
        global.removeEventListener("blur", release);
        document.removeEventListener("visibilitychange", onHidden);
        if (owner === me) owner = null;
      },
    };
  }

  global.cvTalk = { attach: attach, release: releaseGraph, contextError: contextError,
                    clientId: CLIENT_ID };
})(window);
