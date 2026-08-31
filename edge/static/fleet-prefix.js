/* =====================================================================
   CERAVIS fleet per-edge prefix shim — load this FIRST on every /ui page.

   Through the fleet tunnel a page is served at /<edge_id>/ui/… and must call
   /<edge_id>/api (THIS home's edge — frps routes only by URL). We derive that
   prefix from the page's OWN path and transparently prepend it to root-relative
   fetch() calls, so no page needs per-URL edits. On the LAN (served at /ui/…)
   the prefix is empty and nothing changes.

   Live VIDEO is not covered here and must not be: it is served by MediaMTX, not
   by this app, and its URL already carries the edge_id as the MediaMTX path
   (…/<edge_id>/<cam>/whep). live-view.js builds that absolute URL itself.

   Self-contained: defines only window.CERAVIS_PREFIX + wraps fetch, so it never
   clashes with a page's own `api` helper.
   ===================================================================== */
/* Reserve the sidebar layout on <html> BEFORE <body> paints, so navigating
   between pages never visibly jumps (full-width content -> shifted) or flashes
   the collapsed state. renderNav adds the real sidebar + matching body classes
   afterwards. Loaded first in <head> on every sidebar page (NOT the recordings
   console, which doesn't include this file). */
(function () {
  try {
    var de = document.documentElement;
    de.classList.add("cv-shell");
    if (localStorage.getItem("cv-sidebar-collapsed") === "1")
      de.classList.add("cv-shell-collapsed");
    // Already signed in? Mark it so setup.html can skip painting the login gate
    // (no flash of the login before the wizard restores).
    var s = JSON.parse(localStorage.getItem("cv-session") || "null");
    if (s && s.ts && (Date.now() - s.ts) <= 15 * 60 * 1000) de.classList.add("cv-authed");
  } catch (e) {}
})();

(function () {
  var m = location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/);
  var PREFIX = (m && m[1]) || "";
  window.CERAVIS_PREFIX = PREFIX;
  if (!PREFIX) return;                       // LAN / shared /ui — no-op

  function needs(p) {
    return p === "/api" || p.indexOf("/api/") === 0;
  }

  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === "string" && input.charAt(0) === "/" && needs(input)) {
      input = PREFIX + input;
    }
    return _fetch(input, init);
  };
})();
