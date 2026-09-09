
(function (global) {
  "use strict";

  var MTX_WEBRTC_PORT = 8889;
  var CONNECT_TIMEOUT_MS = 9000;
  var STALL_SECS = 8;                 // no fresh frame this long (VISIBLE) = stalled
  var DISCONNECT_GRACE_MS = 5000;     // let a transient ICE "disconnected" self-heal
  var RETRY_MIN_MS = 800;
  var RETRY_MAX_MS = 8000;

  var CV_PREFIX = (location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/) || [])[1] || "";
  var FLEET = !!CV_PREFIX;

  var EDGE = FLEET ? CV_PREFIX.slice(1) : null;
  var edgePromise = null;

  function edgeId() {
    if (EDGE !== null) return Promise.resolve(EDGE);
    if (!edgePromise) {
      edgePromise = fetch("/api/v1/account")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (a) {
          EDGE = (a && (a.edge_id || (a.user && a.user.edgeId))) || "";
          return EDGE;
        })
        .catch(function () { EDGE = ""; return EDGE; });
    }
    return edgePromise;
  }

  function pathName(id) {
    return String(id || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-") || "cam";
  }
  function streamPath(id, edge) {
    var e = String(edge || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-");
    return e ? e + "/" + pathName(id) : pathName(id);
  }

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

  function iceComplete(peer) {
    return new Promise(function (resolve) {
      if (peer.iceGatheringState === "complete") return resolve();
      var t = setTimeout(resolve, 2500);
      peer.addEventListener("icegatheringstatechange", function () {
        if (peer.iceGatheringState === "complete") { clearTimeout(t); resolve(); }
      });
    });
  }

  /* ONE live-video mechanism: MediaMTX WHEP (WebRTC). The connection is treated
     as a long-lived resource — it is NOT torn down when the tab is hidden or the
     window is covered, so returning to the page resumes instantly instead of
     re-negotiating. It only reconnects on a REAL failure (peer failed/closed, a
     "disconnected" that doesn't self-heal within a grace window, or no fresh
     frame for STALL_SECS while the page is visible). */
  function liveView(videoEl, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var stopped = false;
    var connecting = false;
    var pc = null;
    var retry = RETRY_MIN_MS;
    var watchdog = null;
    var graceTimer = null;
    var reconnectTimer = null;
    var lastTime = -1;
    var lastProgress = 0;
    var state = "";

    videoEl.muted = true;
    videoEl.autoplay = true;
    videoEl.playsInline = true;
    videoEl.setAttribute("playsinline", "");

    function setState(s) {
      if (s === state) return;
      state = s;
      onState(s);
    }
    function clearGrace() { if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; } }
    function clearReconnect() { if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; } }
    function closePeer() {
      if (pc) { try { pc.close(); } catch (e) {  } pc = null; }
      try { videoEl.srcObject = null; } catch (e) {  }
    }
    function play() { var p = videoEl.play(); if (p && p.catch) p.catch(function () {  }); }

    // React to connection-state changes AFTER a peer is live.
    function handleConnState(peer) {
      if (pc !== peer) return;                       // a stale peer — ignore
      var s = peer.connectionState;
      if (s === "connected") { clearGrace(); retry = RETRY_MIN_MS; setState("live"); play(); return; }
      if (s === "disconnected") {
        // Transient by nature — ICE often recovers on its own. Show "stalled"
        // but give it a window before tearing the connection down.
        setState("stalled");
        if (!graceTimer) graceTimer = setTimeout(function () {
          graceTimer = null;
          if (pc === peer && peer.connectionState !== "connected") reconnect();
        }, DISCONNECT_GRACE_MS);
        return;
      }
      if (s === "failed" || s === "closed") reconnect();
    }

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
          try { peer.close(); } catch (e) {  }
          if (pc === peer) pc = null;
          reject(new Error(why));
        }
        function ok() {
          if (settled) return;
          settled = true;
          clearTimeout(guard);
          resolve();
        }

        peer.addTransceiver("video", { direction: "recvonly" });
        peer.ontrack = function (e) { videoEl.srcObject = e.streams[0]; };
        peer.onconnectionstatechange = function () {
          var s = peer.connectionState;
          if (s === "connected") { ok(); return; }
          if (!settled) {
            // Pre-connect: "new"/"connecting"/"disconnected" are normal ICE churn
            // — wait for the guard timeout; only a hard end aborts this attempt.
            if (s === "failed" || s === "closed") fail("peer " + s);
            return;
          }
          handleConnState(peer);                     // post-connect: a real drop
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
      if (stopped || connecting) return;
      if (pc && pc.connectionState === "connected") { setState("live"); play(); return; }
      connecting = true;
      clearReconnect();
      setState("connecting");
      try {
        var edge = await edgeId();
        if (stopped) return;
        var path = streamPath(cameraId, edge);
        var order = schemeOrder();
        for (var i = 0; i < order.length; i++) {
          try {
            await attempt(origin(order[i]) + "/" + path + "/whep");
            if (stopped) { closePeer(); return; }
            if (!FLEET) lanSecure = order[i];
            retry = RETRY_MIN_MS;
            lastTime = -1;
            lastProgress = Date.now();
            setState("live");
            play();
            return;
          } catch (e) {
            if (stopped) return;
          }
        }
        setState("offline");
        schedule();
      } finally {
        connecting = false;
      }
    }

    function schedule() {
      if (stopped) return;
      clearReconnect();
      reconnectTimer = setTimeout(connect, retry);
      retry = Math.min(Math.round(retry * 1.7), RETRY_MAX_MS);
    }

    function reconnect() {
      if (stopped) return;
      clearGrace();
      closePeer();
      setState("offline");
      schedule();
    }

    // Tab hidden / window minimized: KEEP the peer alive (WebRTC keeps flowing in
    // the background) so returning is instant. The watchdog is paused meanwhile
    // because a backgrounded <video> stops advancing currentTime — which would
    // otherwise look like a stall and trigger a needless reconnect.
    function onVisibility() {
      if (stopped || document.hidden) return;
      lastTime = -1;                                 // reset the stall baseline
      lastProgress = Date.now();
      clearGrace();
      if (pc && pc.connectionState === "connected") { setState("live"); play(); }
      else if (!connecting) { retry = RETRY_MIN_MS; connect(); }
    }
    document.addEventListener("visibilitychange", onVisibility);

    watchdog = setInterval(function () {
      if (stopped || document.hidden || state !== "live") return;
      var t = videoEl.currentTime;
      if (t !== lastTime) { lastTime = t; lastProgress = Date.now(); return; }
      if ((Date.now() - lastProgress) / 1000 > STALL_SECS) {
        setState("stalled");
        reconnect();
      }
    }, 1000);

    connect();

    return {
      stop: function () {
        stopped = true;
        clearInterval(watchdog);
        clearGrace();
        clearReconnect();
        document.removeEventListener("visibilitychange", onVisibility);
        closePeer();
      },
    };
  }

  global.liveView = liveView;
  /* ONE resolver for "which edge is this page talking to" — the fleet prefix
     when there is one, else the device's own account. Exported because talk.js
     needs the SAME answer for its WebSocket, and two copies of this would drift. */
  global.cvEdgeId = edgeId;
})(window);
