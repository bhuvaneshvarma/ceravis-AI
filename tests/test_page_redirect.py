#!/usr/bin/env python3
"""
The `…/page.html/` -> `…/page.html` 308 never sends a browser to another site.

main._FleetEdgePrefix copies the request path into the Location header. Taken
as-is, `GET //evil.com/login.html/` came back as `Location: //evil.com/login.html`
— a scheme-relative URL every browser follows to evil.com (an open redirect, so
a link that looks like the device's own console lands on a look-alike login).
`/%2Fevil.com/…`, `/%5Cevil.com/…` (the browser reads `\\` as `/`) and
`/%09/evil.com/…` (the browser drops the tab) did the same.

Checked: every hostile path comes back as ONE leading "/" then a printable,
backslash-free path (the shape no browser can read as another host); the
legitimate LAN and fleet redirects keep exactly the Location they had, query
string included; a path that is not `.html/` is never redirected and the fleet
prefix is still stripped; a CR/LF or non-Latin-1 path is a clean 308, not a
dropped connection or a 500.

Pure ASGI, driven by asyncio — no server, no web stack, no network.

    python tests/test_page_redirect.py
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

from configuration import account_config                 # noqa: E402

FAILURES: list[str] = []
EDGE_ID = "E1"
KEY = "fleet-edge-key"

account_config.effective_edge_id = lambda: EDGE_ID


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def _fleet_prefix_class():
    """main._FleetEdgePrefix, taken from main.py's own source: importing main
    would pull the whole AI stack in, and this test must run without it."""
    src = (EDGE / "main.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.ClassDef) and n.name == "_FleetEdgePrefix")
    scope = {"FLEET_EDGE_KEY": KEY}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), scope)
    return scope["_FleetEdgePrefix"]


SEEN: list[dict] = []


async def _app(scope, receive, send):
    SEEN.append(scope)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


MIDDLEWARE = _fleet_prefix_class()(_app)


def request(path: str, qs: bytes = b"") -> tuple[int, str | None]:
    """Status and Location for a GET of the (already URL-decoded) ASGI path."""
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": path, "query_string": qs,
             "raw_path": path.encode("utf-8"), "headers": []}
    SEEN.clear()
    asyncio.run(MIDDLEWARE(scope, receive, send))
    start = sent[0]
    loc = dict(start["headers"]).get(b"location")
    return start["status"], loc.decode("latin-1") if loc is not None else None


# A path-absolute Location the browser can only resolve against THIS host: one
# "/", then no second "/" or "\\", and no tab/CR/LF/space the URL parser strips.
SAME_ORIGIN = re.compile(r"/(?![/\\])[!-~]*")


def same_origin(loc: str | None) -> bool:
    return loc is not None and SAME_ORIGIN.fullmatch(loc) is not None \
        and "\\" not in loc


# --------------------------------------------------------------------------
print("\n1. a crafted path never redirects off-site")
for path in ["//evil.com/login.html/",          # browser-sent, or /%2Fevil.com/…
             "///evil.com/login.html/",         # /%2F%2Fevil.com/…
             "/\\evil.com/login.html/",         # /%5Cevil.com/… — `\\` reads as `/`
             "/\\\\evil.com/login.html/",
             "/\t/evil.com/login.html/",        # /%09/… — the tab is dropped
             "/\r\n/evil.com/login.html/",
             "/ //evil.com/login.html/",
             "//evil.com/E1/ui/live.html/"]:
    status, loc = request(path)
    check(f"{path!r:34} -> 308 {loc!r}", status == 308 and same_origin(loc))
status, loc = request("//evil.com/login.html/", b"x=//evil.com")
check(f"a query string can't move the host either -> {loc!r}",
      status == 308 and loc.startswith("/evil.com/login.html?"))

# --------------------------------------------------------------------------
print("\n2. the legitimate redirects keep exactly the Location they had")
for path, qs, want in [
        ("/ui/live.html/", b"", "/ui/live.html"),
        ("/ui/setup.html/", b"step=2&x=a%20b", "/ui/setup.html?step=2&x=a%20b"),
        ("/E1/ui/live.html/", b"", "/E1/ui/live.html"),
        ("/E1/ui/ui-testing/monitor.html/", b"edge_id=E1",
         "/E1/ui/ui-testing/monitor.html?edge_id=E1"),
        ("/E1/ui//login.html/", b"", "/E1/ui//login.html"),
        ("/ui/my page.html/", b"", "/ui/my%20page.html")]:
    status, loc = request(path, qs)
    check(f"{path + ('?' + qs.decode() if qs else ''):38} -> {loc!r}",
          status == 308 and loc == want)

# --------------------------------------------------------------------------
print("\n3. any other path is passed on, the fleet prefix stripped as before")
status, _ = request("/E1/ui/live.html")
check("/E1/ui/live.html reaches the app as /ui/live.html",
      status == 200 and SEEN and SEEN[0]["path"] == "/ui/live.html"
      and SEEN[0].get(KEY) == EDGE_ID)
status, _ = request("/ui/live.html")
check("/ui/live.html (LAN, no prefix) reaches the app unchanged",
      status == 200 and SEEN and SEEN[0]["path"] == "/ui/live.html"
      and KEY not in SEEN[0])
status, _ = request("/evil.com/ui/live.html")
check("a page path without the .html/ slash is never redirected",
      status == 200 and SEEN and SEEN[0]["path"] == "/evil.com/ui/live.html")

# --------------------------------------------------------------------------
print("\n4. an odd path is a clean redirect, not an error")
for path in ["/€.html/", "/a\r\nSet-Cookie: x=1/b.html/", "/ü/live.html/"]:
    try:
        status, loc = request(path)
        loc.encode("ascii")
        ok = status == 308 and same_origin(loc)
    except Exception as exc:                        # noqa: BLE001
        ok, loc = False, f"raised {type(exc).__name__}: {exc}"
    check(f"{path!r:34} -> 308 {loc!r}", ok)


# --------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All page-redirect checks passed.")
