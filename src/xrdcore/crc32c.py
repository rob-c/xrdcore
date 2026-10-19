"""Castagnoli CRC-32C (poly 0x1EDC6F41, reflected 0x82F63B78).

Used by XRootD paged I/O (every ``kXR_pgread``/``kXR_pgwrite`` page carries
one), by the ``crc32c`` checksum type every storage protocol offers, and by
S3's ``x-amz-checksum-crc32c``. Not the same polynomial as ``zlib.crc32``
(IEEE) - never share a table.

With the ``fast`` extra (``pip install 'xrdcore[fast]'``, which installs
``google-crc32c``) the hardware-assisted C implementation is used - gigabytes
per second. Without it a pure-Python slice-by-eight fallback runs, a few
MB/s: correct, and fine for checksumming a small file, but it is what bounds
a bulk verification. :data:`IS_ACCELERATED` says which one is in use.

The fallback is public as :func:`crc32c_py`, so a test can compare the two
implementations and a benchmark can time the slow path on purpose. The
4 KiB page framing XRootD's paged I/O wraps around it is the XRootD
client's, not this module's.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

__all__ = ["IS_ACCELERATED", "crc32c", "crc32c_py"]

#: The reflected Castagnoli polynomial: CRC-32C is computed LSB-first.
_POLY = 0x82F63B78
_MASK = 0xFFFFFFFF

_Buffer = bytes | bytearray | memoryview


def _build_tables() -> list[list[int]]:
    """The eight slice-by-eight tables: ``T[0]`` is the classic byte table,
    and ``T[k]`` is ``T[k-1]`` advanced over one more zero byte, so that
    eight bytes can be folded with eight independent lookups."""
    first = []
    for n in range(256):
        crc = n
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        first.append(crc)
    tables = [first]
    for k in range(1, 8):
        prev = tables[k - 1]
        tables.append([(v >> 8) ^ first[v & 0xFF] for v in prev])
    return tables


_T = _build_tables()
_T0, _T1, _T2, _T3, _T4, _T5, _T6, _T7 = _T

#: Eight bytes as one little-endian word and four single bytes: the shape
#: slice-by-eight consumes, unpacked in C for the whole buffer at once.
_EIGHT = struct.Struct("<I4B")


def crc32c_py(data: _Buffer, crc: int = 0) -> int:
    """Slice-by-eight software CRC-32C.

    ``struct.iter_unpack`` does the slicing of every 8-byte group in C, which
    is roughly twice as fast as indexing a memoryview per byte; what remains
    per group is eight table lookups.
    """
    view = memoryview(data).cast("B")
    whole = len(view) - len(view) % 8
    c = (crc ^ _MASK) & _MASK
    for word, b4, b5, b6, b7 in _EIGHT.iter_unpack(view[:whole]):
        c ^= word
        c = (
            _T7[c & 0xFF]
            ^ _T6[(c >> 8) & 0xFF]
            ^ _T5[(c >> 16) & 0xFF]
            ^ _T4[c >> 24]
            ^ _T3[b4]
            ^ _T2[b5]
            ^ _T1[b6]
            ^ _T0[b7]
        )
    for byte in view[whole:]:
        c = _T0[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c ^ _MASK


def _accelerated() -> Callable[[_Buffer, int], int] | None:
    """``google-crc32c``'s C implementation, or ``None`` if it is not here.

    The package has its own pure-Python fallback, slower than ours; only its
    C extension (``implementation == "c"``) is worth using.
    """
    try:
        import google_crc32c
    except ImportError:
        return None
    if getattr(google_crc32c, "implementation", "") != "c":
        return None
    extend = google_crc32c.extend

    def fast(data: _Buffer, crc: int = 0) -> int:
        """CRC-32C of ``data``, chained onto ``crc`` (hardware-assisted)."""
        # It takes only ``bytes``; a page is 4 KiB, so the copy is noise.
        return int(extend(crc, data if isinstance(data, bytes) else bytes(data)))

    return fast


_FAST = _accelerated()
#: Whether :func:`crc32c` is the C implementation (the ``fast`` extra).
IS_ACCELERATED = _FAST is not None
_IMPLEMENTATION = _FAST or crc32c_py


def crc32c(data: _Buffer, crc: int = 0) -> int:
    """CRC-32C of ``data``, chained onto ``crc``: ``crc32c(b, crc32c(a)) == crc32c(a + b)``."""
    return _IMPLEMENTATION(data, crc)
