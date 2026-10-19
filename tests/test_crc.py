"""The CRCs against their published vectors and bytewise definitions.

Ported from xrdclient's ``tests/test_crypto.py`` and ``tests/test_crc32c_tail.py``
when :mod:`xrdcore.crc32c`, :mod:`xrdcore.crc64` and :mod:`xrdcore.checksum`
moved here. These have no second implementation to fall back on, so each is
checked against the standard that defines it, and the slice-by-eight tables
against the one-bit-at-a-time definition they speed up.
"""

from __future__ import annotations

import hashlib
import sys
import types
import zlib

import pytest

from xrdcore import crc64 as crc64_module
from xrdcore.checksum import checksum_bytes, checksum_file, new
from xrdcore.crc32c import _accelerated, crc32c, crc32c_py
from xrdcore.crc64 import crc64, crc64nvme

# --------------------------------------------------------------------------
# CRC-32C (Castagnoli) - RFC 3720 appendix B
# --------------------------------------------------------------------------


def test_crc32c_check_value_and_empty_input():
    assert crc32c(b"123456789") == crc32c_py(b"123456789") == 0xE3069283
    assert crc32c(b"") == 0


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (bytes(32), 0x8A9136AA),
        (b"\xff" * 32, 0x62A8AB43),
        (bytes(range(32)), 0x46DD794E),
    ],
)
def test_crc32c_rfc3720_vectors(data, expected):
    assert crc32c(data) == crc32c_py(data) == expected


def test_crc32c_is_chainable_and_is_not_the_ieee_crc32():
    assert crc32c(b"56789", crc32c(b"1234")) == crc32c(b"123456789")
    assert crc32c(b"123456789") != zlib.crc32(b"123456789")


@pytest.mark.parametrize("size", [0, 1, 7, 8, 9, 15, 16, 4096, 4099])
def test_slice_by_eight_agrees_with_the_bytewise_definition(size):
    """Every split between whole 8-byte groups and the trailing bytes, chained too."""
    data = bytes((i * 131 + 7) & 0xFF for i in range(size))
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
    assert crc32c_py(data) == crc ^ 0xFFFFFFFF
    half = size // 2
    assert crc32c_py(memoryview(data)[half:], crc32c_py(bytearray(data[:half]))) == (
        crc ^ 0xFFFFFFFF
    )


@pytest.mark.parametrize("length", [1, 7, 9, 15])
def test_the_portable_crc_handles_a_ragged_tail(length):
    data = b"123456789abcdefg"[:length]
    assert crc32c_py(data[1:], crc32c_py(data[:1])) == crc32c_py(data)


def _fake_crc_module(monkeypatch: pytest.MonkeyPatch, **attributes: object) -> None:
    """Install a stand-in ``google_crc32c`` for :func:`_accelerated` to find."""
    module = types.ModuleType("google_crc32c")
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, "google_crc32c", module)


def test_the_c_extension_is_used_when_present(monkeypatch):
    seen = []

    def extend(crc, chunk):
        seen.append(chunk)
        return crc32c_py(chunk, crc)

    _fake_crc_module(monkeypatch, implementation="c", extend=extend)
    fast = _accelerated()
    assert fast is not None
    data = b"123456789"
    assert fast(data, 0) == 0xE3069283
    assert seen[-1] is data  # what already is bytes is not copied
    assert fast(memoryview(data), 0) == 0xE3069283
    assert type(seen[-1]) is bytes  # a memoryview is: the extension takes bytes only


def test_google_crc32cs_own_python_fallback_is_not_used(monkeypatch):
    _fake_crc_module(monkeypatch, implementation="python", extend=None)
    assert _accelerated() is None


def test_without_the_extra_the_fallback_is_used(monkeypatch):
    monkeypatch.setitem(sys.modules, "google_crc32c", None)  # import raises ImportError
    assert _accelerated() is None


# --------------------------------------------------------------------------
# CRC-64/XZ and CRC-64/NVME
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 5, 8, 13, 64, 1001])
def test_crc64_slice_by_eight_agrees_with_the_bytewise_definition(size):
    data = bytes((i * 37 + 11) & 0xFF for i in range(size))
    for function, poly in ((crc64, 0xC96C5795D7870F42), (crc64nvme, 0x9A6C9329AC4BC9B5)):
        crc = (1 << 64) - 1
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        assert function(data) == crc ^ ((1 << 64) - 1)
        assert function(data[size // 2 :], function(data[: size // 2])) == function(data)
    assert len(crc64_module._XZ) == 8


def test_crc64_xz_answers_to_both_of_its_names():
    """A server registers ``crc64xz`` as an alias of ``crc64``; the digest
    reports the canonical one whichever was asked for."""
    h = new("CRC64-XZ")
    h.update(b"123456789")
    assert h.name == "crc64"
    assert h.hexdigest() == checksum_bytes("crc64", b"123456789")


def test_the_two_crc64s_are_different_checksums():
    """They share their seed and their width and nothing else."""
    assert checksum_bytes("crc64", b"abc") != checksum_bytes("crc64nvme", b"abc")


def test_crc64_of_nothing_is_zero():
    """Seed and final xor are both all ones, so they cancel over no bytes."""
    assert crc64(b"") == crc64nvme(b"") == 0


def test_a_crc64_digest_is_eight_bytes():
    h = new("crc64")
    h.update(b"123456789")
    assert h.value == 0x995DC9BBDF1939FA
    assert h.digest() == (0x995DC9BBDF1939FA).to_bytes(8, "big")


# --------------------------------------------------------------------------
# The checksum surface
# --------------------------------------------------------------------------


def test_checksum_names_are_case_insensitive():
    assert checksum_bytes("ADLER32", b"x") == checksum_bytes("adler32", b"x")


def test_checksum_file_consumes_an_iterable_of_chunks():
    assert checksum_file("sha1", [b"123", b"456789"]) == hashlib.sha1(b"123456789").hexdigest()


def test_checksums_accept_memoryviews():
    h = new("crc32c")
    h.update(memoryview(bytearray(b"123456789")))
    assert h.value == 0xE3069283
    assert h.digest() == (0xE3069283).to_bytes(4, "big")
