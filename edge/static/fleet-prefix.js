/* =====================================================================
   CERAVIS fleet per-edge prefix shim — load this FIRST on every /ui page.

   Through the fleet tunnel a page is served at /<edge_id>/ui/… and must call
   /<edge_id>/api and /<edge_id>/stream (THIS home's edge — frps routes only by
   URL). We derive that prefix from the page's OWN path and transparently prepend
   it to root-relative fetch() calls and same-host /stream|/api WebSockets, so no
   page needs per-URL edits. On the LAN (served at /ui/…) the prefix is empty and
   nothing changes. Self-contained: defines only window.CERAVIS_PREFIX + wraps
   fetch/WebSocket, so it never clashes with a page's own `api` helper.
   ===================================================================== */
(function () {
  var m = location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/);
  var PREFIX = (m && m[1]) || "";
  window.CERAVIS_PREFIX = PREFIX;
  if (!PREFIX) return;                       // LAN / shared /ui — no-op

  function needs(p) {
    return p === "/api" || p.indexOf("/api/") === 0 || p.indexOf("/stream") === 0;
  }

  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === "string" && input.charAt(0) === "/" && needs(input)) {
      input = PREFIX + input;
    }
    return _fetch(input, init);
  };

  var _WS = window.WebSocket;
  function WS(url, protocols) {
    try {
      var u = new URL(url, location.href);
      if (u.host === location.host && needs(u.pathname)) {
        u.pathname = PREFIX + u.pathname;
        url = u.toString();
      }
    } catch (e) { /* leave url as-is */ }
    return protocols === undefined ? new _WS(url) : new _WS(url, protocols);
  }
  WS.prototype = _WS.prototype;
  WS.CONNECTING = _WS.CONNECTING; WS.OPEN = _WS.OPEN;
  WS.CLOSING = _WS.CLOSING; WS.CLOSED = _WS.CLOSED;
  window.WebSocket = WS;
})();
