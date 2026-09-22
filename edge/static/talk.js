/* Push-to-talk: the carer's microphone -> the camera's speaker.

   Three things, kept apart (see edge/talkback/sessions.py):
     * ONE standby connection per page — /api/v1/talkback/session — opened when
       the page loads, kept open, reconnected by itself. It claims nothing and
       blocks nobody; it is how a press reaches the edge with no connection to
       set up, and how the edge tells every page who is speaking.
     * the camera's talk line, which the EDGE keeps open. A press never waits for
       the camera.
     * the FLOOR: a press claims it, release gives it back (the edge keeps it a
       few seconds so the carer can answer), and there is no limit on how long a
       carer talks while holding the button.

   Deliberately press-and-hold. A toggle leaves hot microphones open in living
   rooms; a held button cannot. Releasing it, hiding the tab or letting the
   pointer slip all end the turn.

     mic -> AudioWorklet (resample + A-law, talk-worklet.js) -> this page's
     session -> the edge -> the camera's line

   The microphone and its audio graph stay WARM for a few seconds after a turn,
   because re-acquiring them costs ~300 ms; the worklet is muted between turns,
   so warm never means live. */

(function (global) {
  "use strict";

  var PREFIX = global.CERAVIS_PREFIX ||
    ((location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/) || [])[1] || "");
  var WARM_MS = 8000;             // how long the mic stays open after a turn
  var PING_MS = 20000;            // keeps proxies from idling the standby socket
  var RETRY_MIN_MS = 500;         // reconnect the standby socket: 0.5 s ... 10 s
  var RETRY_MAX_MS = 10000;
  /* Speech that cannot be sent right now (the socket is briefly gone, or slow) is
     already late. Keep only the newest 100 ms of it: the OLDEST frames are the
     ones to lose, because the newest are the words still being said. */
  var OUTBOX_MAX_FRAMES = 5;
  var SOCKET_MAX_BUFFERED = 960;  // ~120 ms of A-law waiting in the socket
  /* Connection-level close codes that mean "do not come back". */
  var FINAL_CLOSE = { 4000: 1, 4401: 1, 4503: 1 };
  /* Refusals whose fix is the camera's password, not pressing again. */
  var PASSWORD_CODES = { unauthorized: 1, no_credential: 1, cooldown: 1 };

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
        // Echo cancellation matters doubly now that listening stays on while
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
        if (session.gain) node.port.postMessage({ type: "gain", value: session.gain });
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

  /* ---- the page's one standby connection -------------------------------- */

  var session = {
    ws: null,
    ready: false,                // welcome received: claims can be sent
    clientId: "pg-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
    buttons: [],                 // every attached talk button
    retry: RETRY_MIN_MS,
    timer: null,
    ping: null,
    gain: 0,
    final: "",                   // set when the edge said "do not come back"
    outbox: [],
  };

  function send(obj) {
    var ws = session.ws;
    if (ws && ws.readyState === 1 && session.ready) { ws.send(JSON.stringify(obj)); return true; }
    return false;
  }

  function flush() {
    var ws = session.ws;
    while (session.outbox.length && ws && ws.readyState === 1 && session.ready &&
           ws.bufferedAmount < SOCKET_MAX_BUFFERED) {
      ws.send(session.outbox.shift());
    }
    while (session.outbox.length > OUTBOX_MAX_FRAMES) session.outbox.shift();
  }

  function each(camera, fn) {
    session.buttons.forEach(function (b) { if (!camera || b.camera === camera) fn(b); });
  }

  /* Answers to THIS page's claims go to the one button that made them: the one
     holding the microphone. A late answer for a camera the carer has already
     left is dropped — their next claim already moved the floor on the edge — and
     must never reach another button: a stray "release" would cut off the room
     the carer is talking to now. */
  function toOwner(camera, fn) {
    if (owner && owner.camera === camera) fn(owner);
  }

  function connect() {
    if (session.ws || session.final || !session.buttons.length) return;
    edgeId().then(function (edge) {
      if (session.ws || !session.buttons.length) return;
      var scheme = location.protocol === "https:" ? "wss://" : "ws://";
      var url = scheme + location.host + PREFIX + "/api/v1/talkback/session" +
        "?client_id=" + encodeURIComponent(session.clientId) +
        (edge ? "&edge_id=" + encodeURIComponent(edge) : "") +
        (global.CV_TALK_NAME ? "&name=" + encodeURIComponent(global.CV_TALK_NAME) : "");
      var ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      session.ws = ws;

      ws.onmessage = function (ev) {
        var m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        dispatch(m);
      };
      ws.onclose = function (ev) {
        if (session.ws !== ws) return;           // an older socket; already replaced
        session.ws = null;
        session.ready = false;
        clearInterval(session.ping);
        if (FINAL_CLOSE[ev.code]) {
          session.final = (ev.reason || "").replace(/^[a-z_]+:\s*/, "") ||
            "Talk-back is not available on this device.";
          each(null, function (b) { b.lost(session.final, true); });
          return;
        }
        each(null, function (b) { b.lost("", false); });
        session.timer = setTimeout(connect, session.retry);
        session.retry = Math.min(Math.round(session.retry * 1.8), RETRY_MAX_MS);
      };
      ws.onerror = function () { /* onclose carries the outcome */ };
    });
  }

  function dispatch(m) {
    if (m.type === "welcome") {
      session.ready = true;
      session.retry = RETRY_MIN_MS;
      session.gain = m.mic_gain || 0;
      if (graph && session.gain) graph.node.port.postMessage({ type: "gain", value: session.gain });
      clearInterval(session.ping);
      session.ping = setInterval(function () { send({ type: "ping" }); }, PING_MS);
      var cams = m.cameras || {};
      Object.keys(cams).forEach(function (cid) {
        if (cvTalk.onCamera) cvTalk.onCamera(cid, cams[cid].readiness);
        each(cid, function (b) { b.floor(cams[cid].floor || { state: "free" }); });
      });
      // A carer still holding the button through a reconnect carries on: the
      // edge kept this page's floor for the floor hold.
      each(null, function (b) { b.resume(); });
    } else if (m.type === "granted") {
      toOwner(m.camera, function (b) { b.granted(); });
    } else if (m.type === "refused") {
      toOwner(m.camera, function (b) { b.refused(m.code, m.message); });
    } else if (m.type === "floor") {
      each(m.camera, function (b) { b.floor(m); });
    } else if (m.type === "taken") {
      toOwner(m.camera, function (b) { b.stopped("Taken over by " + (m.by || "another carer") + "."); });
    } else if (m.type === "released") {
      toOwner(m.camera, function (b) { b.stopped("Stopped: no audio was reaching the device."); });
    } else if (m.type === "camera") {
      if (cvTalk.onCamera) cvTalk.onCamera(m.camera, m.readiness);
    } else if (m.type === "stats") {
      toOwner(m.camera, function (b) { b.health(m); });
    } else if (m.type === "error") {
      session.final = m.message || "Talk-back is not available on this device.";
    }
  }

  /* ---- one camera's talk button ----------------------------------------- */

  function attach(button, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var pressed = false;         // the button is physically down
    var granted = false;         // the edge gave this page the floor
    var mine = false;            // the floor the edge reports is ours

    function setState(s, detail) {
      button.dataset.talk = s;
      onState(s, detail || "");
    }

    function speak() {
      mute(false);
      setState("live");
      flush();
    }

    function quiet() {
      if (owner === me) mute(true);
    }

    var me = {
      camera: cameraId,
      frame: function (d) {
        if (opts.onLevel) opts.onLevel(pressed ? (d.peak || 0) : 0);
        if (!pressed) return;
        session.outbox.push(d.frame);
        if (granted) flush();
        else while (session.outbox.length > OUTBOX_MAX_FRAMES) session.outbox.shift();
      },
      granted: function () {
        granted = true;
        if (pressed) speak();
        else send({ type: "release" });          // let go before the answer came
      },
      refused: function (code, message) {
        pressed = false; granted = false;
        session.outbox.length = 0;
        quiet();
        setState("error", message || "The camera's speaker is not available.");
        if (PASSWORD_CODES[code] && opts.onRefused) opts.onRefused(code);
      },
      stopped: function (why) {
        granted = false;
        session.outbox.length = 0;
        quiet();
        setState("error", why);
        pressed = false;
      },
      floor: function (f) {
        mine = !!(f && f.client_id === session.clientId);
        if (opts.onFloor) opts.onFloor(f.state || "free", f.by || "", mine);
        // The room's live state wins over a past refusal: the refusal's sentence
        // was already shown, and a button still red after the room is free
        // would say the opposite of what is true.
        if (!pressed && !granted)
          setState(f.state && f.state !== "free" && !mine ? "busy" : "idle");
      },
      health: function (m) { if (opts.onHealth) opts.onHealth(m); },
      lost: function (why, final) {
        granted = false;
        if (final) { pressed = false; quiet(); setState("error", why); return; }
        if (pressed) setState("reconnecting");   // keeps capturing: see resume()
        else if (button.dataset.talk !== "error") setState("offline");
      },
      resume: function () {
        if (pressed) { setState("connecting"); send({ type: "claim", camera: cameraId }); }
        else if (button.dataset.talk === "offline") setState("idle");
      },
      hangUp: function () {                      // another button took the mic
        pressed = false; granted = false;
        quiet();
        setState("idle");
      },
    };

    function press() {
      if (pressed) return;
      var problem = session.final || contextError();
      if (problem) { setState("error", problem); return; }
      if (owner && owner !== me) owner.hangUp();  // one microphone, one room
      owner = me;
      pressed = true;
      session.outbox.length = 0;
      setState("connecting");
      acquireGraph().then(function () {
        if (!pressed) return;
        if (granted) speak();
      }).catch(function (err) {
        pressed = false;
        send({ type: "release" });
        setState("error", micError(err));
      });
      if (!send({ type: "claim", camera: cameraId })) {
        setState("reconnecting");               // the claim goes when the socket is back
        connect();
      }
    }

    function release() {
      if (!pressed) return;
      pressed = false;
      quiet();
      if (granted) send({ type: "release" });
      granted = false;
      session.outbox.length = 0;
      setState("idle");
      if (owner === me) keepWarm();
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
    // tab switch. The standby connection itself stays: it claims nothing.
    function onHidden() { if (document.hidden) release(); }
    global.addEventListener("blur", release);
    document.addEventListener("visibilitychange", onHidden);

    session.buttons.push(me);
    setState("idle");
    connect();

    return {
      release: release,
      stop: release,
      isLive: function () { return pressed && granted; },
      isHeld: function () { return pressed || granted; },
      /* The tile is going: end any turn and stop listening to the page. */
      destroy: function () {
        release();
        global.removeEventListener("blur", release);
        document.removeEventListener("visibilitychange", onHidden);
        if (owner === me) owner = null;
        var i = session.buttons.indexOf(me);
        if (i >= 0) session.buttons.splice(i, 1);
        if (!session.buttons.length && session.ws) {
          var ws = session.ws;
          session.ws = null;
          session.ready = false;
          clearInterval(session.ping);
          clearTimeout(session.timer);
          try { ws.send(JSON.stringify({ type: "stop" })); ws.close(); } catch (e) {}
        }
      },
    };
  }

  var cvTalk = { attach: attach, release: releaseGraph, contextError: contextError,
                 /* Set by the page: (cameraId, readiness) whenever the edge says a
                    camera's line changed — so tiles update without polling. */
                 onCamera: null,
                 session: session };
  global.cvTalk = cvTalk;
})(window);
