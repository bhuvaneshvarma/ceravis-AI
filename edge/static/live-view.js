/* =====================================================================
   CERAVIS live view — the ONE mechanism for showing a camera on a screen.

   Every built-in page (live wall, AI monitor, cameras, setup wizard) plays the
   SAME MediaMTX WebRTC stream that the public live links serve. There is no
   second video path: the edge process never encodes, never proxies and never
   re-serves pixels for a viewer.

   Why: the old path decoded the camera in Python, JPEG-encoded every frame on
   the CPU (once per open socket) and pushed it over a WebSocket. At native 4K
   that is ~1-2 MB per frame — hundreds of Mbps demanded of a home uplink and a
   constant CPU tax competing with YOLO. MediaMTX instead fans out the camera's
   ALREADY-COMPRESSED H.264 (~2.5 Mbps, media peer-to-peer over UDP), which is
   why the shared links were flawless while these screens stuttered. Now they
   are the same stream.

   Addressing is DERIVED from the page URL — nothing is configured, nothing is
   stored:

     through the fleet tunnel   /<edge_id>/ui/<page>.html
        live -> /<edge_id>/<cam>/whep     SAME ORIGIN, no port: Caddy holds the
                TLS and frps forwards the path to MediaMTX. Naming a port here
                is what breaks live view — :8889 is not published through the
                tunnel, and http:// on an https:// page is blocked as mixed
                content before it is even tried.

     on the home network        /ui/<page>.html
        live -> <host>:8889/<cam>/whep    MediaMTX direct, the only way to
                reach it with no proxy in front. The device may serve https
                (installer certs) or plain, so both are tried once and the
                winning scheme is remembered.

   The camera's live PATH ('<edge_id>/<cam>' on a provisioned device, else
   '<cam>') mirrors the edge's own stream_path(), so the segment frp routes on
   and the path MediaMTX serves are always the same string.

   Usage:  const v = liveView(videoEl, "LIVING_ROOM", { onState: s => ... });
           v.stop();
   States: "connecting" | "live" | "stalled" | "offline".
   ===================================================================== */
(function (global) {
  "use strict";

  var MTX_WEBRTC_PORT = 8889;          // MediaMTX WHEP (LAN-direct only)
  var CONNECT_TIMEOUT_MS = 9000;       // one WHEP attempt
  var STALL_SECS = 6;                  // video clock frozen this long = stalled
  var RETRY_MIN_MS = 1000;
  var RETRY_MAX_MS = 15000;

  var CV_PREFIX = (location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/) || [])[1] || "";
  var FLEET = !!CV_PREFIX;

  /* This device's edge_id: it IS the tunnel prefix when we came in that way;
     on the LAN the device tells us (once, shared by every tile). */
  var EDGE = FLEET ? CV_PREFIX.slice(1) : null;
  var edgePromise = null;

  function edgeId() {
    if (EDGE !== null) return Promise.resolve(EDGE);
    if (!edgePromise) {
      edgePromise = fetch("/api/v1/account")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (a) {
          // `edge_id` is the canonical value the edge itself routes on; the
          // stored user record is the fallback for an older device.
          EDGE = (a && (a.edge_id || (a.user && a.user.edgeId))) || "";
          return EDGE;
        })
        .catch(function () { EDGE = ""; return EDGE; });
    }
    return edgePromise;
  }

  /* Mirrors the edge's path_name() / stream_path(). */
  function pathName(id) {
    return String(id || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-") || "cam";
  }
  function streamPath(id, edge) {
    var e = String(edge || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-");
    return e ? e + "/" + pathName(id) : pathName(id);
  }

  /* Which scheme MediaMTX answers on. Fleet has exactly one correct answer
     (this page's — the proxy owns TLS). On the LAN we probe once and reuse. */
  var lanSecure = null;
  function schemeOrder() {
    var pageSecure = location.protocol === "https:";
    if (FLEET) return [pageSecure];
    if (lanSecure !== null) return [lanSecure];
    return pageSecure ? [true, false] : [false, true];
  }
  function origin(secure) {
    return FLEET ? location.origin
                 : (secure ? "https" : "http") + "://" + location.hostname
                   + ":" + MTX_WEBRTC_PORT;
  }

  /* Wait until ICE gathering finishes (or 2.5s) so the offer is complete —
     MediaMTX's WHEP endpoint takes one non-trickle SDP. */
  function iceComplete(peer) {
    return new Promise(function (resolve) {
      if (peer.iceGatheringState === "complete") return resolve();
      var t = setTimeout(resolve, 2500);
      peer.addEventListener("icegatheringstatechange", function () {
        if (peer.iceGatheringState === "complete") { clearTimeout(t); resolve(); }
      });
    });
  }

  function liveView(videoEl, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var stopped = false;
    var paused = false;          // page hidden — see the visibility handler
    var pc = null;
    var retry = RETRY_MIN_MS;
    var watchdog = null;
    var lastTime = -1;
    var lastProgress = 0;
    var state = "";

    // Autoplay only works muted + inline. Audio is deliberately off: these are
    // wall/monitor tiles, and a page of unmuted rooms is unusable.
    videoEl.muted = true;
    videoEl.autoplay = true;
    videoEl.playsInline = true;
    videoEl.setAttribute("playsinline", "");

    function setState(s) {
      if (s === state) return;
      state = s;
      onState(s);
    }

    function closePeer() {
      if (pc) { try { pc.close(); } catch (e) { /* already gone */ } pc = null; }
      try { videoEl.srcObject = null; } catch (e) { /* ignore */ }
    }

    /* One WHEP attempt. Resolves when the peer connection reports connected. */
    function attempt(url) {
      return new Promise(function (resolve, reject) {
        var peer = new RTCPeerConnection({
          iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
        });
        pc = peer;
        var settled = false;
        var guard = setTimeout(function () { fail("timeout"); }, CONNECT_TIMEOUT_MS);
        function fail(why) {
          if (settled) return;
          settled = true;
          clearTimeout(guard);
          try { peer.close(); } catch (e) { /* ignore */ }
          if (pc === peer) pc = null;
          reject(new Error(why));
        }
        function ok() {
          if (settled) return;
          settled = true;
          clearTimeout(guard);
          resolve();
        }

        // VIDEO ONLY. These tiles are muted (a wall of unmuted rooms is
        // unusable), so negotiating audio bought nothing — and it was not free:
        // once an audio track exists, the browser slaves video playback to the
        // audio clock, and a camera whose RTP timestamps drift can stall the
        // picture while the pipeline waits for sync. Never ask for a track you
        // will not play. (Two-way audio, if it ever lands, is its own feature
        // with its own negotiation, not a side effect of a wall tile.)
        peer.addTransceiver("video", { direction: "recvonly" });
        peer.ontrack = function (e) { videoEl.srcObject = e.streams[0]; };
        peer.onconnectionstatechange = function () {
          if (peer.connectionState === "connected") return ok();
          if (["failed", "closed", "disconnected"].indexOf(peer.connectionState) >= 0) {
            if (settled) { if (pc === peer) reconnect(); }   // lost after going live
            else fail("peer " + peer.connectionState);
          }
        };

        peer.createOffer()
          .then(function (offer) { return peer.setLocalDescription(offer); })
          .then(function () { return iceComplete(peer); })
          .then(function () {
            return fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/sdp" },
              body: peer.localDescription.sdp,
            });
          })
          .then(function (res) {
            if (!res.ok) throw new Error("WHEP HTTP " + res.status);
            return res.text();
          })
          .then(function (sdp) {
            return peer.setRemoteDescription({ type: "answer", sdp: sdp });
          })
          .catch(function (e) { fail((e && e.message) || "whep error"); });
      });
    }

    async function connect() {
      if (stopped || paused) return;
      setState("connecting");
      var edge = await edgeId();
      if (stopped) return;
      var path = streamPath(cameraId, edge);
      for (var i = 0, order = schemeOrder(); i < order.length; i++) {
        try {
          await attempt(origin(order[i]) + "/" + path + "/whep");
          if (stopped) { closePeer(); return; }
          if (!FLEET) lanSecure = order[i];       // remember what answered
          retry = RETRY_MIN_MS;
          lastTime = -1;
          lastProgress = Date.now();
          setState("live");
          videoEl.play().catch(function () { /* muted autoplay; ignore */ });
          return;
        } catch (e) {
          if (stopped) return;
        }
      }
      setState("offline");
      schedule();
    }

    function schedule() {
      if (stopped || paused) return;
      setTimeout(connect, retry);
      retry = Math.min(Math.round(retry * 1.7), RETRY_MAX_MS);
    }

    function reconnect() {
      if (stopped) return;
      closePeer();
      setState("offline");
      schedule();
    }

    /* Stop decoding video nobody is looking at. A hidden tab keeps its peer
       connections alive, so a backgrounded wall goes on decoding every camera
       for no one — the cost lands hardest on the machine least able to afford
       it. document.hidden tracks tab VISIBILITY, not focus, so a wall left open
       on a second monitor keeps running. */
    function onVisibility() {
      if (stopped) return;
      if (document.hidden) {
        paused = true;
        closePeer();
        setState("offline");
      } else if (paused) {
        paused = false;
        retry = RETRY_MIN_MS;
        connect();
      }
    }
    document.addEventListener("visibilitychange", onVisibility);

    /* Silent-stall guard. A weak link can leave the peer connection "connected"
       while no frames arrive — the picture freezes with no error. The video
       clock stopping is the one reliable signal, so we tear the session down
       and dial again instead of showing a frozen room. */
    watchdog = setInterval(function () {
      if (stopped || state !== "live") return;
      var t = videoEl.currentTime;
      if (t !== lastTime) { lastTime = t; lastProgress = Date.now(); return; }
      var idle = (Date.now() - lastProgress) / 1000;
      if (idle > STALL_SECS) {
        setState("stalled");
        reconnect();
      }
    }, 1000);

    connect();

    return {
      stop: function () {
        stopped = true;
        clearInterval(watchdog);
        document.removeEventListener("visibilitychange", onVisibility);
        closePeer();
      },
    };
  }

  global.liveView = liveView;
})(window);
