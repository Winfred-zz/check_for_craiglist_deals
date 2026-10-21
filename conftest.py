"""Pytest bootstrap: make sibling modules importable + block non-loopback sockets.

conftest runs before any test module imports, so the sys.path insertion
makes the repo-root sibling modules importable when pytest collects tests/,
and the socket guard proves offline tests never touch the network (only the
loopback acquisition tests connect, and those stay on 127.0.0.1).
"""

import socket
import sys
from pathlib import Path

_ = sys.path.insert(0, str(Path(__file__).resolve().parent))

_real_connect = socket.socket.connect


def block_non_loopback_sockets() -> None:
    """Refuse non-loopback connects unless a test explicitly allows them."""
    original = socket.socket.connect

    def connect(self, address):
        host = address[0]
        if host not in ("127.0.0.1", "localhost"):
            raise AssertionError(
                f"offline test attempted non-loopback connect to {host!r}"
            )
        original(self, address)

    socket.socket.connect = connect


block_non_loopback_sockets()
