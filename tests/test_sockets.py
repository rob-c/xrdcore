"""Exactly-N-bytes reads from a stream socket."""

from __future__ import annotations

import pickle
import socket

import pytest

from xrdcore import errors, sockets


class Trickle:
    """A peer that hands over at most ``step`` bytes per ``recv``, then hangs up."""

    def __init__(self, data: bytes, step: int) -> None:
        self.data, self.step, self.calls = data, step, 0

    def recv(self, bufsize: int, /) -> bytes:
        self.calls += 1
        chunk, self.data = (
            self.data[: min(bufsize, self.step)],
            self.data[min(bufsize, self.step) :],
        )
        return chunk


def test_short_recvs_are_joined_and_nothing_past_count_is_read():
    peer = Trickle(b"abcdefgh-rest", step=3)
    assert sockets.recv_exact(peer, 8) == b"abcdefgh"
    assert peer.calls == 3 and peer.data == b"-rest"


def test_zero_bytes_reads_nothing():
    peer = Trickle(b"", step=1)
    assert sockets.recv_exact(peer, 0) == b"" and peer.calls == 0


def test_a_hang_up_part_way_is_a_short_read_that_says_how_far_it_got():
    with pytest.raises(sockets.ShortRead, match="after 3 of 8 bytes") as info:
        sockets.recv_exact(Trickle(b"abc", step=2), 8)
    assert isinstance(info.value, errors.ConnectionError)
    assert isinstance(info.value, OSError)  # what ``except OSError`` callers already catch
    back = pickle.loads(pickle.dumps(info.value))
    assert type(back) is sockets.ShortRead and str(back) == str(info.value)


def test_a_real_socket_pair():
    left, right = socket.socketpair()
    with left, right:
        right.sendall(b"\x00\x00\x00\x05hello")
        right.shutdown(socket.SHUT_WR)
        assert int.from_bytes(sockets.recv_exact(left, 4), "big") == 5
        assert sockets.recv_exact(left, 5) == b"hello"
        with pytest.raises(sockets.ShortRead, match="after 0 of 1 bytes"):
            sockets.recv_exact(left, 1)
