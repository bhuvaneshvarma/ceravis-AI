/* Push-to-talk: the carer's microphone -> the camera's speaker.

   ONE mechanism, and it is deliberately press-and-hold. A toggle leaves hot
   microphones open in living rooms; a held button cannot. Releasing it, losing
   the page, switching tabs or letting the pointer slip all end the turn.

   Shape:
     mic -> AudioWorklet (resample + A-law, talk-worklet.js)
         -> WebSocket /api/v1/talkback/{camera}/stream
         -> the edge -> the camera's own talk endpoint

   The microphone and the audio graph are kept WARM for a few seconds after a
   turn ends, because re-acquiring them costs ~300 ms and an intercom that
   swallows the first word of every sentence is not usable. The worklet is muted
   between turns, so warm never means live: nothing is encoded or sent while the
   button is up. */

(function (global) {
  "use strict";

  var PREFIX = global.CERAVIS_PREFIX ||
    ((location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/) || [])[1] || "");
  var WARM_MS = 8000;             // how long the mic stays open after a turn
  var CONNECT_TIMEOUT_MS = 9000;

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

  /* Why a turn could not start, in words a carer can act on. The mic is a
     browser-security minefield and "NotAllowedError" helps nobody. */
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

  /* The single most common failure, and it looks like a bug rather than a rule:
     browsers refuse getUserMedia outside a secure context, so talk-back works on
     the https:// fleet address and on localhost, and never on a plain
     http://<device-ip> page. Say so before asking for the microphone. */
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

  /* ---- the shared audio graph (one per page, not one per camera) -------- */

  var graph = null;              // { ctx, stream, node, source }
  var warmTimer = null;

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
    if (graph) return Promise.resolve(graph);
    return navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // The camera runs its own AEC, but the room's own echo comes back
        // through the phone's speaker too — ask for all three.
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      },
      video: false,
    }).then(function (stream) {
      // 8 kHz outright where the browser allows it (Chrome, Firefox): the
      // resampler in the worklet then has nothing to do. Safari ignores the
      // hint and the worklet handles the difference.
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
        graph = { ctx: ctx, stream: stream, node: node, source: source };
        return ctx.resume().catch(function () {}).then(function () { return graph; });
      });
    });
  }

  function keepWarm() {
    if (warmTimer) clearTimeout(warmTimer);
    warmTimer = setTimeout(releaseGraph, WARM_MS);
  }

  /* ---- one camera's talk channel ---------------------------------------- */

  /* The session is HELD, not opened per sentence.

     Opening a speaker session costs a TCP connect, a Digest round-trip and the
     camera's own session setup — ~100-300 ms. Paying that on every press is the
     difference between an intercom and a walkie-talkie that eats your first
     syllable. So the first press connects, and every press after it is
     instant: the socket and the camera session stay up, with the microphone
     MUTED in the worklet, until nobody has spoken for the hold window.

     Held is not free — a camera has one speaker, and holding it locks out other
     carers and the Tapo app — so the window is finite and anything that takes
     the page away (tab hidden, window blurred, the tile going) hangs up at once
     rather than sitting on a household's speaker from a backgrounded tab. */

  function attach(button, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var ws = null;
    var pressed = false;         // the button is physically down
    var open = false;            // the socket is up and the camera session held
    // Set SYNCHRONOUSLY, because opening is asynchronous: without it, two quick
    // presses both see a null socket and race two sessions at one camera, and
    // the second is refused as busy by our own first one.
    var connecting = false;
    var connectTimer = null;
    var holdTimer = null;
    var pingTimer = null;
    var holdMs = 75000;          // refreshed from the server's own hold window

    function setState(s, detail, stats) {
      button.dataset.talk = s;
      onState(s, detail || "", stats);
    }

    function clearTimers() {
      if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    }

    function mute(on) {
      if (graph) graph.node.port.postMessage({ type: "mute", value: !!on });
    }

    /* Close the channel and give the camera back. */
    function hangUp(reason) {
      var had = open || pressed;
      pressed = false;
      open = false;
      connecting = false;
      clearTimers();
      mute(true);
      if (graph) graph.node.port.onmessage = null;
      if (ws) {
        var sock = ws;
        ws = null;
        try {
          if (sock.readyState === 1) sock.send(JSON.stringify({ type: "stop" }));
          sock.close();
        } catch (e) {}
      }
      keepWarm();
      if (opts.onTalking) opts.onTalking(false);
      if (had || reason) setState(reason ? "error" : "idle", reason);
    }

    /* Stop speaking but KEEP the camera, so the next press is instant. */
    function release() {
      if (!pressed) return;
      pressed = false;
      mute(true);
      if (opts.onTalking) opts.onTalking(false);
      if (open) {
        setState("ready");
        if (holdTimer) clearTimeout(holdTimer);
        // Hang up just before the server would, so the channel closes on our
        // terms and the user sees "idle" rather than an unexplained drop.
        holdTimer = setTimeout(function () { hangUp(); }, holdMs);
      } else {
        setState("idle");
      }
    }

    function speak() {
      if (!open || !pressed) return;
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      mute(false);
      if (opts.onTalking) opts.onTalking(true);
      setState("live");
    }

    function connect() {
      var problem = contextError();
      if (problem) { setState("error", problem); return; }
      connecting = true;
      setState("connecting");

      Promise.all([acquireGraph(), edgeId()]).then(function (r) {
        var node = r[0].node;
        var edge = r[1];
        if (!connecting || ws) return;             // hung up, or already open
        connecting = false;
        var scheme = location.protocol === "https:" ? "wss://" : "ws://";
        var url = scheme + location.host + PREFIX + "/api/v1/talkback/" +
          encodeURIComponent(cameraId) + "/stream" +
          (edge ? "?edge_id=" + encodeURIComponent(edge) : "");

        ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";

        connectTimer = setTimeout(function () {
          hangUp("The device did not answer. Check the connection and try again.");
        }, CONNECT_TIMEOUT_MS);

        ws.onmessage = function (ev) {
          var msg;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type === "open") {
            if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
            open = true;
            if (msg.mic_gain)
              node.port.postMessage({ type: "gain", value: msg.mic_gain });
            if (msg.hold_secs > 2) holdMs = (msg.hold_secs - 2) * 1000;
            // A held-but-silent socket must survive the proxies in front of it;
            // pings are control frames, so they never touch the server's hold
            // window (only speech does).
            pingTimer = setInterval(function () {
              if (ws && ws.readyState === 1 && !pressed)
                ws.send(JSON.stringify({ type: "ping" }));
            }, 20000);
            if (pressed) speak(); else release();
          } else if (msg.type === "stats") {
            setState(pressed ? "live" : "ready", "", msg);
          } else if (msg.type === "error") {
            hangUp(msg.message || "Talk-back failed.");
          }
        };

        ws.onclose = function (ev) {
          if (!ws) return;                         // our own hangUp
          // The close REASON is the server's sentence; codes only carry a class.
          var why = (ev.reason || "").replace(/^[a-z_]+:\s*/, "");
          if (!why && ev.code !== 1000 && ev.code !== 1001) {
            why = ev.code === 4409 ? "Someone else is already speaking to this camera."
              : ev.code === 4401 ? "This device did not accept the request."
              : ev.code === 4503 ? "Talk-back is switched off on this device."
              : "The talk connection closed.";
          }
          hangUp(why);
        };

        ws.onerror = function () { /* onclose carries the outcome */ };

        node.port.onmessage = function (ev) {
          var d = ev.data;
          if (!d || d.type !== "audio") return;
          if (opts.onLevel) opts.onLevel(pressed ? (d.peak || 0) : 0);
          if (pressed && open && ws && ws.readyState === 1) {
            // Never let a slow link queue speech: past a second of backlog the
            // words being buffered are already stale, so drop them instead.
            if (ws.bufferedAmount > 8000) return;
            ws.send(d.frame);
          }
        };
      }).catch(function (err) {
        pressed = false;
        connecting = false;
        keepWarm();
        setState("error", micError(err));
      });
    }

    function press() {
      if (pressed) return;
      pressed = true;
      if (open) speak();                           // held channel: instant
      else if (!ws && !connecting) connect();      // first press: ~200 ms
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

    // Anything that takes the page away hangs up — a hot mic must not survive a
    // tab switch, and a backgrounded tab must not sit on a household's speaker.
    // Named, because these outlive the button: a tile that goes away has to be
    // able to take its listeners with it (destroy).
    function onHidden() { if (document.hidden) hangUp(); }
    global.addEventListener("blur", release);
    document.addEventListener("visibilitychange", onHidden);

    setState("idle");
    return {
      stop: hangUp,
      release: release,
      isLive: function () { return pressed && open; },
      isHeld: function () { return open; },
      /* Hang up AND stop listening to the page. Called when the tile goes. */
      destroy: function () {
        global.removeEventListener("blur", release);
        document.removeEventListener("visibilitychange", onHidden);
        hangUp();
      },
    };
  }

  global.cvTalk = { attach: attach, release: releaseGraph, contextError: contextError };
})(window);
