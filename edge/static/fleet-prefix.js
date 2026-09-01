

(function () {
  try {
    var de = document.documentElement;
    de.classList.add("cv-shell");
    if (localStorage.getItem("cv-sidebar-collapsed") === "1")
      de.classList.add("cv-shell-collapsed");

    var s = JSON.parse(localStorage.getItem("cv-session") || "null");
    var authed = !!(s && s.ts);
    var onLogin = /login\.html$/.test(location.pathname);
    // monitor.html is a standalone diagnostics console — never gated by login.
    var ungated = /(?:login|monitor)\.html$/.test(location.pathname);
    if (authed) {
      de.classList.add("cv-authed");
      if (onLogin) location.replace("live.html");
    } else if (!ungated) {
      location.replace("login.html");
    }
  } catch (e) {}
})();

(function () {
  var m = location.pathname.match(/^(\/[^/]+)\/ui(?:\/|$)/);
  var PREFIX = (m && m[1]) || "";
  window.CERAVIS_PREFIX = PREFIX;
  if (!PREFIX) return;

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
