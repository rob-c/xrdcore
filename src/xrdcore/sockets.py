"""Reading a framed stream socket: exactly so many bytes, or a clear failure.

Every length-prefixed protocol on the stack - Kerberos over TCP (RFC 4120
section 7.2.2), dCache's dcap movers, and the fakes that answer them - reads
"the next N bytes" in a loop, because ``recv`` may return fewer than asked
for. :func:`recv_exact` is that loop, once.

A peer that hangs up part-way is :class:`ShortRead`, an
:class:`xrdcore.errors.ConnectionError` and so an :class:`OSError`: code that
already treats a dropped connection like a timeout (try the next KDC, end
the mover quietly) catches it with the ``except OSError`` it has.
"""

from __future__ import annotations

from typing import Protocol

from .errors import ConnectionError

__all__ = ["Receiver", "ShortRead", "recv_exact"]


class Receiver(Protocol):
    """What :func:`recv_exact` reads from: a stream socket, plain or TLS."""

    def recv(self, bufsize: int, /) -> bytes:
        """Up to ``bufsize`` bytes; ``b""`` once the peer has closed its side."""
        ...


class ShortRead(ConnectionError):
    """The peer closed the connection before sending all it had announced."""


def recv_exact(sock: Receiver, count: int) -> bytes:
    """Exactly ``count`` bytes from ``sock``; :class:`ShortRead` if it closes first.

    The message says how far the read got (``after 3 of 8 bytes``), which is
    what tells a truncated frame from a peer that never answered.
    """
    out = bytearray()
    while len(out) < count:
        chunk = sock.recv(count - len(out))
        if not chunk:
            raise ShortRead(f"the peer closed the connection after {len(out)} of {count} bytes")
        out += chunk
    return bytes(out)
