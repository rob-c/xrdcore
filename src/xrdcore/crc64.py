"""The two 64-bit CRCs XRootD servers report, both reflected and both seeded
and finished with all ones.

``crc64`` is CRC-64/XZ (ECMA-182, normal polynomial ``0x42F0E1EBA9EA3693``) -
what ``xrdcrc64`` computes and what ``kXR_Qcksum`` and a WebDAV ``Digest``
header call ``crc64``. ``crc64nvme`` is CRC-64/NVME (Rocksoft, normal
polynomial ``0xAD93D23594C93659``), the one S3 spells
``x-amz-checksum-crc64nvme``; a server offers it under its own name, never as
``crc64``, because the two disagree on every input.

Stock XRootD computes neither, so a checksum in one of these names comes from
a gateway - and this is what verifies it without one.

Both are pure Python, slice-by-eight: a few MB/s, which is what a checksum
verification after a transfer costs. No widely packaged accelerator covers
them - ``google-crc32c`` is CRC-32C only, and the CRC-64/NVME in ``awscrt``
is a large native dependency to take for one checksum - so there is no
optional fast path; a site that verifies large files by ``crc64`` should
prefer ``adler32`` or ``crc32c`` where the server offers them.
"""

from __future__ import annotations

import struct

__all__ = ["crc64", "crc64nvme"]

_MASK = (1 << 64) - 1


def _tables(reflected_poly: int) -> tuple[list[int], ...]:
    """The eight slice-by-eight tables: ``T[0]`` is the byte table, ``T[k]``
    is ``T[k-1]`` advanced one more zero byte."""
    first = []
    for n in range(256):
        crc = n
        for _ in range(8):
            crc = (crc >> 1) ^ reflected_poly if crc & 1 else crc >> 1
        first.append(crc)
    tables = [first]
    for _ in range(7):
        tables.append([(value >> 8) ^ first[value & 0xFF] for value in tables[-1]])
    return tuple(tables)


#: Reflections of 0x42F0E1EBA9EA3693 and 0xAD93D23594C93659.
_XZ = _tables(0xC96C5795D7870F42)
_NVME = _tables(0x9A6C9329AC4BC9B5)

_WORD = struct.Struct("<Q")


def _fold(tables: tuple[list[int], ...], data: bytes | bytearray | memoryview, crc: int) -> int:
    """Slice-by-eight: each little-endian 64-bit word is folded in with eight lookups."""
    t0, t1, t2, t3, t4, t5, t6, t7 = tables
    view = memoryview(data).cast("B")
    whole = len(view) - len(view) % 8
    c = ~crc & _MASK
    for (word,) in _WORD.iter_unpack(view[:whole]):
        c ^= word
        c = (
            t7[c & 0xFF]
            ^ t6[(c >> 8) & 0xFF]
            ^ t5[(c >> 16) & 0xFF]
            ^ t4[(c >> 24) & 0xFF]
            ^ t3[(c >> 32) & 0xFF]
            ^ t2[(c >> 40) & 0xFF]
            ^ t1[(c >> 48) & 0xFF]
            ^ t0[c >> 56]
        )
    return ~_bytewise(t0, view[whole:], c) & _MASK


def _bytewise(table: list[int], view: memoryview, c: int) -> int:
    """The classic one-lookup-per-byte loop, for the last ``len % 8`` bytes."""
    for byte in view:
        c = table[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c


def crc64(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """CRC-64/XZ of ``data``, chained onto ``crc``.

    ``crc`` is the running value in the form it is reported in, so chaining
    two chunks gives what one call over both would::

        >>> crc64(b"9", crc64(b"12345678")) == crc64(b"123456789")
        True
    """
    return _fold(_XZ, data, crc)


def crc64nvme(data: bytes | bytearray | memoryview, crc: int = 0) -> int:
    """CRC-64/NVME of ``data``, chained onto ``crc``."""
    return _fold(_NVME, data, crc)
