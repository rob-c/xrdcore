"""Checksum algorithms, by the names storage servers use.

Every algorithm exposes the incremental ``hashlib``-ish shape so a copy can
digest as it streams:

    >>> h = new("adler32")
    >>> h.update(b"hello")
    >>> h.hexdigest()
    '062c0215'

Two families sit behind one surface. The CRCs and Adler-32 are "native":
a step function folds each chunk into an integer register, and the hex form
is that integer zero-padded to the algorithm's width - the spelling
``kXR_Qcksum`` answers with and ``xrdadler32`` prints (``adler32`` of an empty
file is ``00000001``, not ``1``). The cryptographic hashes are
:mod:`hashlib`'s, wrapped to look the same. Callers never need to know which
they hold.

Names are XRootD's (``adler32``, ``crc32``, ``crc32c``, ``crc64``, ``md5``...)
because the same string goes on the wire in ``kXR_query`` checksum requests
and is compared with what servers answer; HTTP's spellings (RFC 3230 /
RFC 9530: ``sha-256``, ``adler``) are mapped onto these by
:mod:`xrdhttp.webdav`. A client with a naming policy of its own - gfal2's
narrower algorithm set, its ``UNIXcksum`` alias - layers it over :func:`new`
rather than forking the digests, so every tool computes the same function.
"""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .crc32c import crc32c
from .crc64 import crc64, crc64nvme

__all__ = [
    "Checksum",
    "ChecksumInfo",
    "Digest",
    "algorithms",
    "checksum_bytes",
    "checksum_file",
    "new",
]


@dataclass(frozen=True, slots=True)
class ChecksumInfo:
    """A finished checksum as a client reports it: an algorithm name and its hex value.

    One type for every source - a ``root://`` checksum query, a WebDAV
    ``Digest`` header, a Metalink ``<hash>`` - so checksums from different
    protocols compare equal when they describe the same bytes. It lives here,
    below xrdhttp and the clients, because each of them reports checksums and
    two look-alike classes would never compare equal to each other.
    """

    algorithm: str
    value: str

    def __str__(self) -> str:
        """``algorithm:value``, the form ``xrdcp --cksum`` and gfal2 accept and print."""
        return f"{self.algorithm}:{self.value}"


#: One chunk folded into a running value - the shape every algorithm here has.
Step = Callable[["bytes | bytearray | memoryview", int], int]


class Checksum:
    """Incremental digest with a stable lowercase hex representation."""

    __slots__ = ("_step", "_value", "_width", "name")

    def __init__(self, name: str, step: Step, seed: int, width: int) -> None:
        """Start a digest at ``seed`` that ``step`` advances; ``width`` is in hex digits.

        The seed is the algorithm's own initial register (``1`` for
        Adler-32, ``0`` for the CRCs, whose pre- and post-inversion live
        inside ``step``). Use :func:`new` rather than building one by hand.
        """
        self.name = name
        self._step = step
        self._value = seed
        self._width = width

    def update(self, data: bytes | bytearray | memoryview) -> None:
        """Fold ``data`` into the running value; chunking never changes the result."""
        self._value = self._step(data, self._value)

    @property
    def value(self) -> int:
        """The running value as an integer, as ``zlib.adler32`` would return it."""
        return self._value

    def hexdigest(self) -> str:
        """Lower-case hex, zero-padded to the algorithm's full width."""
        return f"{self._value:0{self._width}x}"

    def digest(self) -> bytes:
        """The value as big-endian bytes - the order the hex form is written in."""
        return self._value.to_bytes(self._width // 2, "big")

    def __repr__(self) -> str:
        """Name and current hex value."""
        return f"Checksum({self.name!r}, {self.hexdigest()})"


class _HashlibChecksum:
    """Adapter giving a ``hashlib`` object the same surface as `Checksum`."""

    __slots__ = ("_h", "name")

    def __init__(self, name: str) -> None:
        """Wrap a fresh :func:`hashlib.new` object for ``name``."""
        self.name = name
        self._h = hashlib.new(name)

    def update(self, data: bytes | bytearray | memoryview) -> None:
        """Feed ``data`` to the hash."""
        self._h.update(data)

    @property
    def value(self) -> int:
        """The digest so far as a big-endian integer, for parity with :class:`Checksum`."""
        return int.from_bytes(self._h.digest(), "big")

    def hexdigest(self) -> str:
        """:mod:`hashlib`'s lower-case hex digest; already full width."""
        return self._h.hexdigest()

    def digest(self) -> bytes:
        """:mod:`hashlib`'s raw digest bytes."""
        return self._h.digest()

    def __repr__(self) -> str:
        """Name and current hex value, spelt as :class:`Checksum` spells it."""
        return f"Checksum({self.name!r}, {self.hexdigest()})"


#: Either kind of digest :func:`new` returns.
Digest = Checksum | _HashlibChecksum

#: ``name -> (step, seed, width in hex digits)`` for the non-hashlib family.
_NATIVE: dict[str, tuple[Callable[..., int], int, int]] = {
    "adler32": (zlib.adler32, 1, 8),
    "crc32": (zlib.crc32, 0, 8),
    "crc32c": (crc32c, 0, 8),
    "crc64": (crc64, 0, 16),
    "crc64nvme": (crc64nvme, 0, 16),
}
#: The one alias a server registers: CRC-64/XZ answers to both names.
_ALIASES = {"crc64xz": "crc64"}
#: The :mod:`hashlib` names offered: all in ``hashlib.algorithms_guaranteed``,
#: so :func:`algorithms` is the same on every platform.
_HASHLIB = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")


def algorithms() -> tuple[str, ...]:
    """Every algorithm this build can compute."""
    return tuple(sorted(_NATIVE)) + _HASHLIB


def new(name: str) -> Digest:
    """A fresh incremental digest for ``name`` (case-insensitive).

    Dashes are ignored, so HTTP's ``SHA-256`` and XRootD's ``sha256`` are
    one algorithm. Raises :class:`ValueError` naming what is available for
    any other name - a typo must not silently pick a different algorithm.
    """
    key = name.lower().replace("-", "")
    key = _ALIASES.get(key, key)
    if key in _NATIVE:
        step, seed, width = _NATIVE[key]
        return Checksum(key, step, seed, width)
    if key in _HASHLIB:
        return _HashlibChecksum(key)
    raise ValueError(f"unknown checksum algorithm {name!r}; have {algorithms()}")


def checksum_bytes(name: str, data: bytes) -> str:
    """Hex digest of ``data``."""
    h = new(name)
    h.update(data)
    return h.hexdigest()


def checksum_file(name: str, chunks: Iterable[bytes]) -> str:
    """Hex digest of a stream of chunks."""
    h = new(name)
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()
