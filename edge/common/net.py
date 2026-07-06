from __future__ import annotations

"""One answer to "what is this device's LAN IP" — the UDP-connect trick (no
traffic is sent; it just resolves the outbound route). Used by the startup
banner and tests/test_cloud."""

import socket


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None
