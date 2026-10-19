"""Checksums: the catalogue vectors, chaining, naming and the two CRC-32C paths."""

from __future__ import annotations

import hashlib
import zlib

import pytest

from xrdcore import checksum, crc32c, crc64

CHECK = b"123456789"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("adler32", "091e01de"),
        ("crc32", "cbf43926"),
        ("crc32c", "e3069283"),
        ("crc64", "995dc9bbdf1939fa"),
        ("crc64xz", "995dc9bbdf1939fa"),
        ("crc64nvme", "ae8b14860a799888"),
        ("md5", hashlib.md5(CHECK).hexdigest()),
        ("SHA-256", hashlib.sha256(CHECK).hexdigest()),
    ],
)
def test_catalogue_check_values(name, expected):
    assert checksum.checksum_bytes(name, CHECK) == expected


def test_native_values_are_zero_padded_to_full_width():
    assert checksum.checksum_bytes("adler32", b"") == "00000001"
    assert checksum.checksum_bytes("crc32", b"") == "00000000"
    assert checksum.new("crc64").hexdigest() == "0" * 16


@pytest.mark.parametrize("name", checksum.algorithms())
def test_chunking_never_changes_the_result(name):
    data = bytes(range(256)) * 37
    whole = checksum.checksum_bytes(name, data)
    pieces = [data[i : i + 13] for i in range(0, len(data), 13)]
    assert checksum.checksum_file(name, pieces) == whole


@pytest.mark.parametrize("name", checksum.algorithms())
def test_digest_and_value_agree_with_hexdigest(name):
    digest = checksum.new(name)
    digest.update(CHECK)
    assert digest.digest().hex() == digest.hexdigest()
    assert digest.value == int(digest.hexdigest(), 16)
    assert digest.name in repr(digest)


def test_native_value_matches_zlib():
    digest = checksum.new("adler32")
    digest.update(CHECK)
    assert digest.value == zlib.adler32(CHECK)


def test_unknown_names_are_refused_with_the_list():
    with pytest.raises(ValueError, match="have"):
        checksum.new("sha3-512")


def test_checksum_info_prints_the_xrdcp_form_and_compares_by_value():
    """One reported checksum: ``algorithm:value``, equal when both fields are, frozen."""
    info = checksum.ChecksumInfo("adler32", "062c0215")
    assert str(info) == "adler32:062c0215"
    assert info == checksum.ChecksumInfo("adler32", "062c0215")
    assert info != checksum.ChecksumInfo("crc32c", "062c0215")
    with pytest.raises(AttributeError):
        info.value = "0"  # type: ignore[misc]  # assigning a frozen field is the refusal under test


def test_algorithms_are_stable_and_cover_both_families():
    names = checksum.algorithms()
    assert names[:5] == ("adler32", "crc32", "crc32c", "crc64", "crc64nvme")
    assert "sha512" in names


def test_crc_chaining():
    assert crc32c.crc32c(b"9", crc32c.crc32c(b"12345678")) == crc32c.crc32c(CHECK)
    assert crc64.crc64(b"9", crc64.crc64(b"12345678")) == crc64.crc64(CHECK)
    assert crc64.crc64nvme(b"9", crc64.crc64nvme(b"12345678")) == crc64.crc64nvme(CHECK)


@pytest.mark.parametrize("length", [0, 1, 7, 8, 9, 15, 16, 4096, 4099])
def test_pure_python_crc32c_agrees_with_the_selected_implementation(length):
    data = bytes((i * 31 + 7) & 0xFF for i in range(length))
    assert crc32c.crc32c_py(data) == crc32c.crc32c(data)
    assert crc32c.crc32c_py(memoryview(data)) == crc32c.crc32c(bytearray(data))


def test_accelerated_flag_is_a_bool():
    assert isinstance(crc32c.IS_ACCELERATED, bool)
