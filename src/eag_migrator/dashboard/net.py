"""Port selection for the dashboard.

8080 is the most contended port on a developer machine — every second app
grabs it. The default here is deliberately off the beaten track, it is
configurable, and a clash is reported plainly instead of surfacing as a
traceback from deep inside the server.
"""

from __future__ import annotations

import os
import socket

DEFAULT_PORT = 19080
"""Not a default for any common tool, and clear of this project's other
published ports (13306, 13307, 15432, 15433, 18080)."""

PORT_ENV = "EAGM_DASHBOARD_PORT"


def configured_port() -> int:
    raw = (os.getenv(PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we bind here right now?

    SO_REUSEADDR lets a socket reuse a port left in TIME_WAIT, but not one an
    active listener holds — so this still detects a real clash.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "0.0.0.0" else "", port))  # noqa: S104
            return True
        except OSError:
            return False


def find_free(host: str = "127.0.0.1", near: int | None = None) -> int:
    """A port that is free right now.

    Tries a few just above the preferred one first, so the suggestion stays
    recognisably part of this project, and only then asks the OS for anything.
    """
    if near:
        for candidate in range(near + 1, near + 12):
            if candidate <= 65535 and is_free(candidate, host):
                return candidate

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host if host != "0.0.0.0" else "", 0))  # noqa: S104
        return int(sock.getsockname()[1])
