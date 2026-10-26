"""
The web server (edge/serve.py): one app, two listeners.

1. the redirect rule (pure ASGI): a LAN browser on plain HTTP is sent to the
   same address on HTTPS; this device itself, the tunnel (X-Forwarded-For) and
   anything while HTTPS is down are served as they are
2. the real thing, on free ports with a throwaway certificate: both listeners
   answer, the app starts ONCE, a LAN address on plain HTTP gets the 308, and
   one stop shuts both down

Needs openssl for part 2 (Git Bash has it); part 1 runs anywhere.

Run:  PYTHONPATH=edge python tests/test_serve.py
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

import serve                                                 # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


starts = []


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                starts.append(time.time())
                await send({"type": "lifespan.startup.complete"})
            elif m["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    body = f"{scope.get('scheme')} {scope['path']}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def run(wrapper, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    asyncio.run(wrapper(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    close = next((m for m in sent if m["type"] == "websocket.close"), None)
    hdrs = dict(start["headers"]) if start else {}
    return (start or {}).get("status"), hdrs.get(b"location", b"").decode(), close


def req(client, path="/ui/live.html", headers=(), kind="http", qs=b""):
    return {"type": kind, "path": path, "query_string": qs, "client": (client, 5555),
            "headers": [(b"host", b"192.168.0.50:8000"), *headers], "scheme": "http"}


print("\n0. the process starts with the math libraries capped")
import os                                                    # noqa: E402
check("one worker thread per math library, set before numpy can load",
      os.environ.get("OMP_NUM_THREADS") == "1" and os.environ.get("OPENBLAS_NUM_THREADS") == "1"
      and os.environ.get("OMP_WAIT_POLICY") == "PASSIVE" and "numpy" not in sys.modules)

print("\n1. the redirect rule")
up = {"v": True}
w = serve.LanToHttps(app, 8443, lambda: up["v"])
st, loc, _ = run(w, req("192.168.0.20", qs=b"cam=LOUNGE"))
check("a LAN browser -> 308 to the same page on HTTPS",
      st == 308 and loc == "https://192.168.0.50:8443/ui/live.html?cam=LOUNGE", f"{st} {loc}")
check("this device itself -> served", run(w, req("127.0.0.1"))[0] == 200)
check("the tunnel (a loopback proxy's X-Forwarded-For) -> served",
      run(w, req("203.0.113.9", headers=[(b"x-forwarded-for", b"203.0.113.9")]))[0] == 200)
up["v"] = False
check("HTTPS not up -> the LAN is served on plain HTTP, never sent nowhere",
      run(w, req("192.168.0.20"))[0] == 200)
up["v"] = True
check("a LAN websocket on plain HTTP is refused", run(w, req("192.168.0.20", kind="websocket"))[2]
      is not None)
v6 = req("fd00::20")
v6["headers"] = [(b"host", b"[fd00::5]:8000")]
check("an IPv6 host stays bracketed", run(w, v6)[1].startswith("https://[fd00::5]:8443/"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))             # no packet is sent
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def get(url, verify=True):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(url, timeout=5) as r:
            return r.status, r.read().decode(), r.headers.get("location")
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers.get("location")


print("\n2. both listeners for real")
openssl = shutil.which("openssl")
if not openssl:
    print("  (openssl not available — skipped)")
else:
    certs = Path(tempfile.mkdtemp(prefix="ceravis-certs-"))
    crt, key = certs / "server.crt", certs / "server.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(crt), "-subj", "/CN=test"],
                   check=True, capture_output=True)
    p1, p2 = free_port(), free_port()
    server = serve.WebServer(app, http_port=p1, https_port=p2, cert=(crt, key))
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=lambda: loop.run_until_complete(server.run()), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not server.https_up():
        time.sleep(0.1)
    check("both listeners up", server.https_up())
    check("plain HTTP from this device -> served",
          get(f"http://127.0.0.1:{p1}/x")[:2] == (200, "http /x"))
    check("HTTPS -> served, the app sees https",
          get(f"https://127.0.0.1:{p2}/x", verify=False)[:2] == (200, "https /x"))
    check("the app started once, not once per listener", len(starts) == 1, str(len(starts)))
    ip = lan_ip()
    if ip:
        st, _, loc = get(f"http://{ip}:{p1}/ui/live.html")
        check("plain HTTP from a LAN address -> 308 to HTTPS",
              st == 308 and loc == f"https://{ip}:{p2}/ui/live.html", f"{st} {loc}")
    else:
        print("  (no LAN address on this machine — LAN redirect check skipped)")
    server.http.should_exit = True
    t.join(timeout=20)
    check("one stop shuts both down", not t.is_alive())
    shutil.rmtree(certs, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All web server checks passed.")
