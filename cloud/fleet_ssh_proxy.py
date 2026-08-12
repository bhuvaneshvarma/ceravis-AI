#!/usr/bin/env python3
"""HTTP-CONNECT helper for fleet SSH — the ProxyCommand for `ssh <user>@<edge_id>`.

Every house's SSH is multiplexed through ONE frps port (`tcpmuxHTTPConnectPort`)
and routed by the HTTP CONNECT host = that device's `edge_id` — the same token
that keys its live links. Reaching it needs a client that speaks HTTP CONNECT.
`ncat`/`corkscrew` do, but neither ships with Windows (a missing one is the
`CreateProcessW failed error:2` / `posix_spawnp: No such file or directory` that
ssh reports before it ever touches the network). This script is that client, in
the standard library only, so the admin box needs nothing installed.

It is a pipe, not a service: connect to frps, send CONNECT <edge_id>:22, then
copy bytes both ways between the socket and stdin/stdout. The SSH session inside
stays end-to-end encrypted — frps sees only the `edge_id` it routes on.

    # one-off
    ssh -o ProxyCommand="python cloud/fleet_ssh_proxy.py --via EC2_IP:7001 %h %p" \
        ceravis@<edge_id>

    # daily driver — ~/.ssh/config, then just `ssh house-a`
    Host house-a
        HostName     <edge_id>            # the CONNECT host frps routes on
        User         ceravis
        ProxyCommand python C:/path/to/cloud/fleet_ssh_proxy.py --via EC2_IP:7001 %h %p

    # sanity check, no ssh involved — proves frps -> frpc -> Jetson sshd
    python cloud/fleet_ssh_proxy.py --via EC2_IP:7001 --check <edge_id>

`--via` defaults to $CERAVIS_FLEET_SSH_VIA, so it can be set once per admin box.
Run it from the ADMIN machine (that is what it tests: your IP through the EC2
security group). `python3` on Ubuntu/Debian, `python` on Windows.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading

DEFAULT_TCPMUX_PORT = 7001
DEFAULT_SSH_PORT = 22
BUF = 65536


def _err(msg: str) -> None:
    """Diagnostics go to stderr - stdout is the SSH byte stream."""
    print(f"fleet-ssh: {msg}", file=sys.stderr, flush=True)


def _hostport(value: str, default_port: int) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep:
        return value, default_port
    if not port.isdigit():
        raise SystemExit(f"fleet-ssh: bad host:port {value!r}")
    return host, int(port)


def _read_head(sock: socket.socket) -> bytes:
    """Read exactly the CONNECT response head, leaving the tunnel bytes queued.

    Byte-at-a-time on purpose: the moment frp answers 200 the Jetson's sshd
    banner follows on the same socket, and over-reading here would swallow it.
    """
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            return bytes(head)
        head += chunk
        if len(head) > 16384:
            raise SystemExit("fleet-ssh: CONNECT response head too large - not an frp tcpmux port?")
    return bytes(head)


def connect(via: str, edge_id: str, port: int, timeout: float) -> socket.socket:
    """Open the tunnel to <edge_id>:<port> through the frps tcpmux port."""
    proxy_host, proxy_port = _hostport(via, DEFAULT_TCPMUX_PORT)
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    except OSError as exc:
        raise SystemExit(
            f"fleet-ssh: cannot reach frps at {proxy_host}:{proxy_port} ({exc}).\n"
            f"  - is `tcpmuxHTTPConnectPort = {proxy_port}` uncommented in frps.toml, frps restarted?\n"
            f"  - is TCP {proxy_port} open to THIS machine's IP in the EC2 security group?"
        ) from None

    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    target = f"{edge_id}:{port}"
    sock.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
    )

    try:
        head = _read_head(sock)
    except socket.timeout:
        sock.close()
        raise SystemExit(
            f"fleet-ssh: frps accepted the connection but never answered CONNECT within {timeout:g}s."
        ) from None

    routing_hint = (
        f"  frps has no tcpmux route registered for {edge_id!r} right now.\n"
        f"  A correct frpc.toml does NOT prove registration - the authority is the\n"
        f"  RUNNING state, so read the logs, in this order:\n"
        f"    EC2 : sudo journalctl -u frps --no-pager | grep -iE 'tcpmux|ceravis-ssh' | tail\n"
        f"    edge: sudo systemctl restart frpc && sudo journalctl -u frpc -n 40 --no-pager\n"
        f"  A proxy that failed to register STAYS failed until frpc restarts - so if\n"
        f"  frpc started while frps still had tcpmuxHTTPConnectPort commented out,\n"
        f"  the block reads perfectly and nothing is registered. Restarting frpc is\n"
        f"  the fix. Also check customDomains is the BARE edge_id: no leading slash\n"
        f"  (that is the live-link `locations` shape, and it 404s here)."
    )
    if not head:
        sock.close()
        raise SystemExit(
            f"fleet-ssh: frps closed the connection without answering CONNECT.\n"
            f"  That means no edge is registered under edge_id {edge_id!r}.\n{routing_hint}"
        )

    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if b" 200" not in head.split(b"\r\n", 1)[0]:
        sock.close()
        raise SystemExit(
            f"fleet-ssh: frps refused the tunnel - {status}\n{routing_hint}"
        )

    sock.settimeout(None)
    return sock


def check(sock: socket.socket, edge_id: str) -> int:
    """Prove the whole chain: read the sshd banner frp is now piping through."""
    sock.settimeout(5.0)
    try:
        banner = sock.recv(256)
    except socket.timeout:
        banner = b""
    finally:
        sock.close()

    if banner.startswith(b"SSH-"):
        version = banner.split(b"\r")[0].split(b"\n")[0].decode("latin-1", "replace")
        _err(f"OK - {edge_id} reachable end-to-end; sshd says {version}")
        return 0
    if not banner:
        _err(
            f"tunnel to {edge_id} opened but nothing answered on port 22 -\n"
            f"  frps->frpc is fine; sshd on the Jetson is down, or the ceravis-ssh\n"
            f"  proxy's localPort is not 22 (`sudo systemctl status ssh` on the edge)."
        )
        return 1
    _err(f"tunnel opened but the peer is not sshd (first bytes: {banner[:40]!r})")
    return 1


def pump(sock: socket.socket) -> None:
    """Copy bytes both ways until either end closes."""

    def stdin_to_sock() -> None:
        fd = sys.stdin.fileno()
        try:
            while True:
                data = os.read(fd, BUF)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)  # let the far end see our EOF
            except OSError:
                pass

    threading.Thread(target=stdin_to_sock, daemon=True).start()

    out = sys.stdout.buffer
    try:
        while True:
            data = sock.recv(BUF)
            if not data:
                break
            out.write(data)
            out.flush()
    except OSError:
        pass
    finally:
        sock.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fleet_ssh_proxy.py",
        description="HTTP-CONNECT ProxyCommand for CERAVIS fleet SSH (frp tcpmux).",
    )
    ap.add_argument("edge_id", help="the device's edge_id - ssh substitutes %%h")
    ap.add_argument("port", nargs="?", type=int, default=DEFAULT_SSH_PORT,
                    help="port on the edge - ssh substitutes %%p (default 22)")
    ap.add_argument("--via", default=os.environ.get("CERAVIS_FLEET_SSH_VIA"),
                    help=f"frps host[:port] of tcpmuxHTTPConnectPort "
                         f"(default port {DEFAULT_TCPMUX_PORT}); env CERAVIS_FLEET_SSH_VIA")
    ap.add_argument("--check", action="store_true",
                    help="test the path end-to-end and exit (no ssh session)")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="seconds to wait for connect + CONNECT reply (default 15)")
    args = ap.parse_args(argv)

    if not args.via:
        ap.error("--via HOST[:PORT] is required (or set CERAVIS_FLEET_SSH_VIA)")

    sock = connect(args.via, args.edge_id, args.port, args.timeout)
    if args.check:
        return check(sock, args.edge_id)
    pump(sock)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
