"""Pipeline cases ported from xrdclient's tests/test_shared_engines.py.

Partial writes, retained views, stop races, consumer failures and the bounded
exit of a read-ahead worker stuck in a read.
"""

from __future__ import annotations

import io
import queue
import threading
from types import SimpleNamespace

import pytest

from xrdcore import pipeline


@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("recycle", [False, True])
def test_pipeline_handles_partial_writes_without_losing_or_duplicating_bytes(depth, recycle):
    source = b"abcdefghijklmnopqrstuvwxyz"
    target = bytearray()
    progress = []

    class PartialWriter:
        def write(self, view):
            accepted = min(len(view), 3)
            target.extend(view[:accepted])
            return accepted

    assert pipeline.pump(
        io.BytesIO(source),
        PartialWriter(),
        len(source),
        8,
        lambda done, total: progress.append((done, total)),
        None,
        depth,
        recycle=recycle,
    ) == len(source)
    assert target == source
    assert progress == [(8, 26), (16, 26), (24, 26), (26, 26)]


@pytest.mark.parametrize("count", [-1, 0, 10])
def test_invalid_write_counts_raise_the_adapter_error(count):
    failure = RuntimeError("destination did not accept the chunk")
    writer = SimpleNamespace(write=lambda view: count)
    with pytest.raises(RuntimeError) as caught:
        pipeline.write_all(writer, memoryview(b"abc"), lambda: failure)
    assert caught.value is failure


def test_zero_write_default_is_an_io_error():
    with pytest.raises(OSError, match="destination stopped accepting"):
        pipeline.write_all(SimpleNamespace(write=lambda view: 0), memoryview(b"abc"))


def test_legacy_writer_without_a_return_value_is_supported():
    received = []
    pipeline.write_all(
        SimpleNamespace(write=lambda view: received.append(bytes(view))), memoryview(b"abc")
    )
    assert received == [b"abc"]


def test_retained_views_are_not_overwritten_by_read_ahead():
    views = []

    class RetainingWriter:
        def write(self, view):
            views.append(view)
            return len(view)

    pipeline.pump(
        io.BytesIO(b"abcdefghijkl"), RetainingWriter(), None, 3, None, None, 2, recycle=False
    )
    assert b"".join(views) == b"abcdefghijkl"


def test_stop_while_waiting_for_a_buffer_does_not_issue_another_read():
    ahead = pipeline.ReadAhead(io.BytesIO(b"abc"), 3, 2)

    class StoppingQueue(queue.Queue[bytearray]):
        def get(self, block: bool = True, timeout: float | None = None) -> bytearray:
            ahead._stop.set()
            return bytearray(3)

    ahead._buffers = StoppingQueue()
    ahead._read()
    assert list(ahead._drain()) == []


def test_pre_stopped_reader_never_reads():
    ahead = pipeline.ReadAhead(io.BytesIO(b"abc"), 3, 2)
    ahead._stop.set()
    with ahead as pieces:
        assert list(pieces) == []


@pytest.mark.parametrize("failure_at", ["writer", "progress", "digest"])
def test_consumer_failure_preserves_exception_and_joins_reader(failure_at):
    failure = RuntimeError(failure_at)

    def failed(*args):
        raise failure

    writer = SimpleNamespace(write=failed if failure_at == "writer" else lambda view: len(view))
    digest = SimpleNamespace(update=failed if failure_at == "digest" else lambda view: None)
    progress = failed if failure_at == "progress" else None
    with pytest.raises(RuntimeError) as caught:
        pipeline.pump(
            io.BytesIO(b"a" * 100),
            writer,
            None,
            4,
            progress,
            digest,
            2,
            thread_name="shared-engine-test",
        )
    assert caught.value is failure
    assert not any(thread.name == "shared-engine-test" for thread in threading.enumerate())


# -- read-ahead: recycled buffers, bounded exit ----------------------------------


def test_read_ahead_recycles_its_buffers_by_default():
    seen = set()

    class Noting:
        def write(self, view):
            seen.add(id(view.obj))
            return len(view)

    pipeline.pump(io.BytesIO(b"x" * 300), Noting(), None, 10, None, None, 2)
    assert len(seen) <= 3  # depth buffers (and the wake-up spare), not one per chunk


class _Stuck:
    """A source whose read blocks until it is closed or aborted."""

    def __init__(self, *, closable: bool = True, abortable: bool = False) -> None:
        self.released = threading.Event()
        self.reading = threading.Event()
        self.stopped_by: list[str] = []
        if closable:
            self.close = lambda: self._stop("close")
        if abortable:
            self.abort = lambda: self._stop("abort")

    def _stop(self, how: str) -> None:
        self.stopped_by.append(how)
        self.released.set()

    def read(self, size: int) -> bytes:
        self.reading.set()
        self.released.wait(10)
        return b""


@pytest.fixture
def quick_stop(monkeypatch):
    monkeypatch.setattr(pipeline, "STOP_GRACE", 0.05)
    monkeypatch.setattr(pipeline, "STOP_TIMEOUT", 2.0)


@pytest.mark.parametrize(("abortable", "expected"), [(False, "close"), (True, "abort")])
def test_leaving_early_unblocks_a_reader_stuck_in_a_read(quick_stop, abortable, expected):
    source = _Stuck(abortable=abortable)
    ahead = pipeline.ReadAhead(source, 4, 2, thread_name="stuck-reader")
    with pytest.raises(RuntimeError), ahead:
        source.reading.wait(5)
        raise RuntimeError("the consumer gave up")
    assert source.stopped_by == [expected]
    assert not ahead._thread.is_alive()


def test_a_reader_that_will_not_stop_is_left_behind_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(pipeline, "STOP_GRACE", 0.01)
    monkeypatch.setattr(pipeline, "STOP_TIMEOUT", 0.05)
    source = _Stuck(closable=False)
    with (
        caplog.at_level("WARNING", logger="xrdcore.pipeline"),
        pipeline.ReadAhead(source, 4, 2, thread_name="stubborn-reader"),
    ):
        source.reading.wait(5)
    assert "stubborn-reader did not stop" in caplog.text
    source.released.set()


def test_an_abort_that_fails_is_only_logged(quick_stop, caplog):
    source = _Stuck(closable=False)

    def failing():
        source.released.set()
        raise OSError("already closed")

    source.close = failing
    with (
        caplog.at_level("DEBUG", logger="xrdcore.pipeline"),
        pipeline.ReadAhead(source, 4, 2, thread_name="failing-abort"),
    ):
        source.reading.wait(5)
    deadline = threading.Event()
    deadline.wait(0.1)
    assert "aborting the source of failing-abort failed" in caplog.text
