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

  /* ---- what a dropped channel is allowed to do ---------------------------
     A carer mid-sentence whose phone changes cell must not have to notice,
     diagnose and re-press. The channel comes back by itself, and says so while
     it is trying. It gives up after REJOIN_WINDOW_MS because a microphone that
     silently reattaches minutes later is its own hazard. */
  var REJOIN_WINDOW_MS = 15000;   // total time we keep trying to get back
  var REJOIN_MIN_MS = 300;        // first retry, then 1.8x each time
  var REJOIN_MAX_MS = 3000;

  /* Close codes that mean "do not come back": the answer will not change by
     asking again, and retrying would only make a household's speaker contended.
       4401 the device did not accept us      4503 talk-back is switched off
       4409 somebody ELSE is holding the room (our OWN stale session is handed
            back by the device instead — see client_id below) */
  var FINAL_CODES = { 4401: 1, 4409: 1, 4503: 1, 1000: 1, 1001: 1 };

  /* 4500 is "the camera handshake failed", and the device puts WHICH failure in
     front of the reason ("unauthorized: …"). Some are a network blip worth
     another try; these are not — the answer is the same however often we ask.
     Retrying a refused password is also exactly what a camera locks an account
     out for. This is the bug that showed a wrong password as "Reconnecting…". */
  var FINAL_REASONS = { unauthorized: 1, no_credential: 1, no_camera: 1,
                        no_host: 1, refused: 1, protocol: 1, busy: 1, disabled: 1 };
  function reasonCode(reason) {
    var m = /^([a-z_]+):/.exec(reason || "");
    return m ? m[1] : "";
  }

  /* The outbound queue, in FRAMES of 20 ms. Speech that cannot be sent now is
     speech that is already late; past this much backlog the oldest frames are
     the ones to lose, because the newest are the words still being said. */
  var OUTBOX_MAX_FRAMES = 5;      // 100 ms
  var SOCKET_MAX_BUFFERED = 960;  // ~120 ms of A-law waiting in the socket

  /* This browser's id for one microphone. It is how a carer reclaims their OWN
     session after a drop instead of being refused by a socket the device has
     not yet noticed is dead. Minted per attach, never persisted. */
  var clientSeq = 0;
  function mintClientId() {
    clientSeq += 1;
    return "c" + Date.now().toString(36) + "-" + clientSeq +
           "-" + Math.random().toString(36).slice(2, 8);
  }

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
    if (graph) {
      // A warm graph is not necessarily a RUNNING one. Browsers suspend an
      // AudioContext when a tab is backgrounded, and one created outside a user
      // gesture starts suspended — either way the worklet stops being scheduled
      // and the carer talks into a microphone that produces nothing. Resume on
      // every acquire, not only on the one that built it.
      if (graph.ctx.state !== "running") {
        return graph.ctx.resume().catch(function () {}).then(function () { return graph; });
      }
      return Promise.resolve(graph);
    }
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

    // Reconnect state. `rejoinUntil` is a deadline, not a counter, so a fast
    // link gets many attempts and a slow one gets fewer — both stop at the same
    // wall-clock moment, which is the thing a carer actually experiences.
    var clientId = mintClientId();
    var rejoinTimer = null;
    var rejoinDelay = REJOIN_MIN_MS;
    var rejoinUntil = 0;
    var rejoining = false;
    // Frames the socket has not taken yet. See OUTBOX_MAX_FRAMES.
    var outbox = [];

    function setState(s, detail, stats) {
      button.dataset.talk = s;
      onState(s, detail || "", stats);
    }

    function clearTimers() {
      if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      if (rejoinTimer) { clearTimeout(rejoinTimer); rejoinTimer = null; }
    }

    /* Hand the socket as much of the backlog as it will take without building
       one of its own. Anything still queued past OUTBOX_MAX_FRAMES is dropped
       from the FRONT: the oldest frames are the stalest words, and keeping them
       would push everything the carer says next further behind. */
    function flush() {
      while (outbox.length && ws && ws.readyState === 1 &&
             ws.bufferedAmount < SOCKET_MAX_BUFFERED) {
        ws.send(outbox.shift());
      }
      while (outbox.length > OUTBOX_MAX_FRAMES) outbox.shift();
    }

    function mute(on) {
      if (graph) graph.node.port.postMessage({ type: "mute", value: !!on });
    }

    /* Close the channel and give the camera back. */
    function hangUp(reason) {
      var had = open || pressed || rejoining;
      pressed = false;
      open = false;
      connecting = false;
      rejoining = false;
      rejoinUntil = 0;
      outbox.length = 0;
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
      // Let go mid-reconnect and there is no turn left worth restoring: stop
      // chasing the camera rather than grabbing a household's speaker for
      // somebody who has already finished speaking.
      if (rejoining) { hangUp(); return; }
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

    /* Try to get the channel back. Returns false once the window has closed,
       and the caller then fails the turn for real.

       The microphone is deliberately NOT muted while this runs: the worklet
       keeps filling the outbox, so the last 100 ms of what the carer is saying
       survives the gap and lands the moment the socket returns. Everything
       older than that is dropped, because it would arrive as a sentence the
       room has already moved past. */
    function scheduleRejoin() {
      var now = Date.now();
      if (!rejoinUntil) rejoinUntil = now + REJOIN_WINDOW_MS;
      if (now >= rejoinUntil) return false;
      rejoining = true;
      open = false;
      clearTimers();
      setState("reconnecting");
      rejoinTimer = setTimeout(function () {
        rejoinTimer = null;
        if (!rejoining || ws || connecting) return;
        connect();
      }, rejoinDelay);
      rejoinDelay = Math.min(Math.round(rejoinDelay * 1.8), REJOIN_MAX_MS);
      return true;
    }

    /* Ask the device the same question over plain HTTPS, and say which link
       actually broke.

       A WebSocket is uniquely bad at explaining itself: a proxy that refuses
       the upgrade, a tunnel that is down and a device that is off all arrive as
       close code 1006 with an EMPTY reason. Nobody holding a phone can tell
       those apart, and the logs that could are on a device in someone's house.

       So when the socket fails without a sentence, we run the SILENT test
       endpoint — the same one a technician runs at handover, which proves
       reachability, credential and firmware without making a sound — and the
       three outcomes are three different faults:

         test OK          the device and camera are fine; the WEBSOCKET path is
                          blocked (a proxy or network that does not pass them)
         test errors      the device is reachable and is telling us exactly what
                          is wrong with the camera; show its own words
         test unreachable the device is not answering at all */
    function diagnose(fallback) {
      return edgeId().then(function (edge) {
        return fetch(PREFIX + "/api/v1/talkback/" +
                     encodeURIComponent(cameraId) + "/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ edgeId: edge }),
        });
      }).then(function (r) {
        return r.json().then(function (b) { return b; },
                             function () { return null; })
          .then(function (body) {
            if (r.ok) {
              return "The camera answered a silent test in " +
                ((body && body.elapsed_ms) || "?") + " ms, so this device and " +
                "this camera are both fine. The live audio connection itself " +
                "was blocked — that is a proxy or network in between that does " +
                "not allow WebSockets.";
            }
            var d = (body && body.detail) || {};
            if (r.status === 401 || r.status === 409)
              return "This device did not accept the request (its edge_id does " +
                     "not match the address this page was opened on).";
            if (r.status === 503)
              return "Talk-back is switched off on this device (TALKBACK_ENABLED).";
            return d.message || fallback;
          });
      }).catch(function () {
        return fallback + " The device did not answer a test either, so it is " +
               "unreachable from here rather than refusing.";
      });
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
      setState(rejoining ? "reconnecting" : "connecting");

      Promise.all([acquireGraph(), edgeId()]).then(function (r) {
        var node = r[0].node;
        var edge = r[1];
        if (!connecting || ws) return;             // hung up, or already open
        connecting = false;
        var scheme = location.protocol === "https:" ? "wss://" : "ws://";
        var url = scheme + location.host + PREFIX + "/api/v1/talkback/" +
          encodeURIComponent(cameraId) + "/stream" +
          "?client_id=" + encodeURIComponent(clientId) +
          (edge ? "&edge_id=" + encodeURIComponent(edge) : "");

        ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";

        connectTimer = setTimeout(function () {
          connectTimer = null;
          // Drop the socket FIRST, so its own onclose sees a handle we have
          // already given up on and does not schedule a second rejoin.
          if (ws) { try { ws.close(); } catch (e) {} ws = null; }
          connecting = false;
          if (rejoining && scheduleRejoin()) return;
          hangUp("The device did not answer. Check the connection and try again.");
        }, CONNECT_TIMEOUT_MS);

        ws.onmessage = function (ev) {
          var msg;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type === "open") {
            if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
            open = true;
            // Back on the air. Reset the ladder so the NEXT drop, whenever it
            // comes, gets its own fast first retry rather than inheriting the
            // backoff this one ended on.
            rejoining = false;
            rejoinUntil = 0;
            rejoinDelay = REJOIN_MIN_MS;
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
            // queued_ms is the honest latency number: the carer's voice still
            // sitting in the device's socket to the camera, unheard.
            setState(pressed ? "live" : "ready", "", msg);
            if (opts.onHealth) opts.onHealth(msg);
          } else if (msg.type === "error") {
            hangUp(msg.message || "Talk-back failed.");
          }
        };

        ws.onclose = function (ev) {
          if (!ws) return;                         // our own hangUp
          ws = null;
          connecting = false;
          var wasOpen = open;
          open = false;

          // A drop worth chasing is one the answer could change for: a network
          // blip, not a refusal. FINAL_CODES are the refusals, plus the device
          // closing a channel on schedule — asking again would only take a
          // household's speaker back off whoever the device just gave it to.
          var code = reasonCode(ev.reason);
          var final = !!FINAL_CODES[ev.code] || !!FINAL_REASONS[code];
          if (!final && (pressed || wasOpen) && scheduleRejoin()) return;
          if (FINAL_REASONS[code] && code !== "busy" && opts.onRefused)
            opts.onRefused(code);

          // The close REASON is the server's sentence; codes only carry a class.
          var why = (ev.reason || "").replace(/^[a-z_]+:\s*/, "");
          var vague = !why;
          if (!why && ev.code !== 1000 && ev.code !== 1001) {
            why = ev.code === 4409 ? "Someone else is already speaking to this camera."
              : ev.code === 4401 ? "This device did not accept the request."
              : ev.code === 4503 ? "Talk-back is switched off on this device."
              : rejoining ? "Lost the connection to this camera and could not get it back."
              : "The talk connection closed.";
            vague = (ev.code !== 4409 && ev.code !== 4401 && ev.code !== 4503);
          }
          hangUp(why);
          // The socket had nothing to say. Go and find out, then replace the
          // message — toast() shows one at a time, so the vague sentence is
          // superseded rather than stacked on.
          if (vague && why) {
            diagnose(why).then(function (better) {
              if (better && better !== why) setState("error", better);
            });
          }
        };

        ws.onerror = function () { /* onclose carries the outcome */ };

        node.port.onmessage = function (ev) {
          var d = ev.data;
          if (!d || d.type !== "audio") return;
          if (opts.onLevel) opts.onLevel(pressed ? (d.peak || 0) : 0);
          if (pressed && (open || rejoining)) {
            // Straight into the outbox, never straight at the socket. A slow
            // link, or a link that is briefly gone, must cost the OLDEST words
            // rather than the newest ones — the previous version dropped the
            // frame in hand and kept a second of stale speech queued ahead of
            // it, which is the wrong way round for a live voice.
            outbox.push(d.frame);
            flush();
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

    // There is deliberately NO warm-on-hover. It was tried and removed: opening
    // the microphone without a user gesture leaves an AudioContext SUSPENDED
    // (so a later press captures silence), can raise a permission prompt
    // because a mouse crossed a tile, and leaves the microphone live with
    // nothing scheduled to release it. Saving ~300 ms on the first press only
    // is not worth any of those. The press warms it, and WARM_MS keeps it warm
    // for the rest of the conversation.

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
