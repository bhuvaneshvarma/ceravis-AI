(function () {
  try {
    var de = document.documentElement;
    de.classList.add("cv-shell");
    if (localStorage.getItem("cv-sidebar-collapsed") === "1")
      de.classList.add("cv-shell-collapsed");

    var s = JSON.parse(localStorage.getItem("cv-session") || "null");
    var authed = !!(s && s.ts);
    var onLogin = /login\.html$/.test(location.pathname);
    // Pages anyone may open: the sign-in page itself, the first-run setup
    // status screen and the standalone diagnostics console. (The diagnostics
    // console's data still needs a signed-in browser — the server decides.)
    var ungated = /(?:login|monitor|welcome)\.html$/.test(location.pathname);
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
  // The server is the authority on sign-in (api/auth): when it answers 401 the
  // browser's own note is stale — drop it and go to the sign-in page.
  var signInPage = /(?:login|welcome)\.html$/.test(location.pathname);

  function needs(p) {
    return p === "/api" || p.indexOf("/api/") === 0;
  }

  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (PREFIX && typeof input === "string" && input.charAt(0) === "/" && needs(input)) {
      input = PREFIX + input;
    }
    return _fetch(input, init).then(function (r) {
      if (r.status === 401 && !signInPage) {
        try { localStorage.removeItem("cv-session"); } catch (e) {}
        location.replace("login.html");
      }
      return r;
    });
  };
})();
