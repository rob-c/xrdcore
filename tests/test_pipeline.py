"""The byte pump: both read modes, short writes, errors and bounded shutdown."""

from __future__ import annotations

import errno
import hashlib
import io
import threading

import pytest

from xrdcore import pipeline
from xrdcore.cancel import CancelScope, current, within

DATA = bytes(range(256)) * 1000


class Trickle:
    """A writer that takes at most ``limit`` bytes per call."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.out = bytearray()

    def write(self, data: memoryview) -> int:
        piece = data[: self.limit]
        self.out += piece
        return len(piece)


class Legacy:
    """A writer that returns ``None``, as old plugin streams do."""

    def __init__(self) -> None:
        self.out = bytearray()

    def write(self, data: memoryview) -> None:
        self.out += data


class ReadOnly:
    """A source with ``read`` only, so :func:`fill` copies."""

    def __init__(self, data: bytes) -> None:
        self.stream = io.BytesIO(data)

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


@pytest.mark.parametrize("depth", [1, 4])
@pytest.mark.parametrize("recycle", [True, False])
def test_pump_copies_digests_and_reports_progress(depth, recycle):
    sink = Trickle(1000)
    digest = hashlib.sha256()
    seen = []
    done = pipeline.pump(
        io.BytesIO(DATA),
        sink,
        len(DATA),
        4096,
        lambda d, t: seen.append((d, t)),
        digest,
        depth,
        recycle=recycle,
    )
    assert done == len(DATA)
    assert bytes(sink.out) == DATA
    assert digest.hexdigest() == hashlib.sha256(DATA).hexdigest()
    assert seen[-1] == (len(DATA), len(DATA))


def test_pump_without_readinto_or_byte_count():
    sink = Legacy()
    assert pipeline.pump(ReadOnly(DATA), sink, None, 1000, None, None) == len(DATA)
    assert bytes(sink.out) == DATA


class Stuck:
    def write(self, data: memoryview) -> int:
        return 0


class Greedy:
    def write(self, data: memoryview) -> int:
        return len(data) + 1


@pytest.mark.parametrize("writer", [Stuck(), Greedy()])
def test_a_writer_that_stops_accepting_is_an_eio(writer):
    with pytest.raises(OSError) as info:
        pipeline.pump(io.BytesIO(DATA), writer, None, 100, None, None)
    assert info.value.errno == errno.EIO


def test_short_write_factory_is_used():
    with pytest.raises(LookupError):
        pipeline.write_all(Stuck(), memoryview(b"x"), lambda: LookupError("full"))


class Failing:
    def read(self, size: int) -> bytes:
        raise ConnectionResetError("gone")


@pytest.mark.parametrize("depth", [1, 3])
def test_a_reader_error_reaches_the_caller(depth):
    with pytest.raises(ConnectionResetError):
        pipeline.pump(Failing(), Legacy(), None, 100, None, None, depth)


def test_readahead_reads_in_the_callers_context():
    scope = CancelScope()
    seen = []

    class Watching:
        def __init__(self) -> None:
            self.stream = io.BytesIO(b"abc")

        def read(self, size: int) -> bytes:
            seen.append(current())
            return self.stream.read(size)

    within(scope, lambda: pipeline.pump(Watching(), Legacy(), None, 1, None, None, 2))
    assert seen and all(s is scope for s in seen)


def test_a_stuck_worker_is_aborted_then_abandoned(monkeypatch):
    monkeypatch.setattr(pipeline, "STOP_GRACE", 0.01)
    monkeypatch.setattr(pipeline, "STOP_TIMEOUT", 0.01)
    release = threading.Event()
    aborted = threading.Event()

    class Hung:
        first = True

        def read(self, size):
            if self.first:
                self.first = False
                return b"x"
            release.wait(5)
            return b""

        def abort(self):
            aborted.set()

    reader = Hung()
    with pipeline.ReadAhead(reader, 1, 2) as pieces:
        assert bytes(next(pieces)) == b"x"
    assert aborted.wait(5)
    release.set()


def test_abort_falls_back_to_close_and_swallows_errors():
    closed = threading.Event()

    class Closer:
        def read(self, size):
            return b""

        def close(self):
            closed.set()
            raise OSError("already closed")

    pipeline._abort(Closer(), "t")
    assert closed.wait(5)
    pipeline._abort(object(), "t")  # neither: nothing to do
