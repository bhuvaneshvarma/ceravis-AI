
(function (global) {
  "use strict";

  var MTX_WEBRTC_PORT = 8889;
  var CONNECT_TIMEOUT_MS = 9000;
  var STALL_SECS = 6;
  var RETRY_MIN_MS = 1000;
  var RETRY_MAX_MS = 15000;

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

  function liveView(videoEl, cameraId, opts) {
    opts = opts || {};
    var onState = opts.onState || function () {};
    var stopped = false;
    var paused = false;
    var pc = null;
    var retry = RETRY_MIN_MS;
    var watchdog = null;
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

    function closePeer() {
      if (pc) { try { pc.close(); } catch (e) {  } pc = null; }
      try { videoEl.srcObject = null; } catch (e) {  }
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
          if (peer.connectionState === "connected") return ok();
          if (["failed", "closed", "disconnected"].indexOf(peer.connectionState) >= 0) {
            if (settled) { if (pc === peer) reconnect(); }
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
          if (!FLEET) lanSecure = order[i];
          retry = RETRY_MIN_MS;
          lastTime = -1;
          lastProgress = Date.now();
          setState("live");
          videoEl.play().catch(function () {  });
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
