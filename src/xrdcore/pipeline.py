"""Shared, bounded byte-transfer pipeline; endpoint policy lives in adapters.

Views are valid until the consumer asks for the next chunk: by default the
buffers are *recycled*, so a transfer of any size costs ``depth`` chunk
buffers and no allocation per chunk. A consumer that keeps a view beyond
that - a caller's own stream that queues what it is handed rather than
copying it - must say ``recycle=False``, which gives every chunk a buffer of
its own. No retry happens here: replay safety belongs to the endpoint and job
policy.

Every streamed copy - XRootD's, gfal2's, an HTTP or S3 download - ends in
:func:`pump`: a :class:`Reader` (a remote file, a local file, a caller's
stream) and a :class:`Writer` are joined here, optionally with a
:class:`ReadAhead` worker so the next network read overlaps the current local
write. The
worker thread's lifetime is the ``with`` block's: it must never outlive the
transfer by more than :data:`STOP_GRACE` + :data:`STOP_TIMEOUT`, and it runs
in a copy of the caller's :mod:`contextvars` so deadlines and cancel scopes
reach its reads.
"""

from __future__ import annotations

import contextvars
import errno
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import Any, Protocol, cast

from .log import get_logger

_log = get_logger(__name__)

#: How long :meth:`ReadAhead.__exit__` waits for its worker to notice the
#: stop on its own - long enough for a read already landing to finish.
STOP_GRACE = 1.0

#: How long it then waits once the source has been told to abort, before it
#: stops waiting and leaves the (daemon) worker to finish by itself.
STOP_TIMEOUT = 10.0


class Reader(Protocol):
    """Any byte source; a ``readinto`` method, if present, is used instead (see :func:`fill`)."""

    def read(self, size: int) -> bytes:
        """Up to ``size`` bytes; ``b""`` only at end of data."""
        ...


class Writer(Protocol):
    """Any byte sink, including ones that take only part of what they are given."""

    def write(self, data: memoryview) -> int | None:
        """Bytes accepted from ``data``, or ``None`` for "all of it" (legacy sinks)."""
        ...


def fill(reader: Reader, buffer: bytearray) -> int:
    """Read once into ``buffer``; the count, ``0`` at end of data.

    ``readinto`` when the reader has it - our own files land bytes straight
    in the recycled buffer - else ``read`` plus one copy. One call, not a
    loop: a short read is passed on as a short chunk rather than waited out.
    """
    readinto = getattr(reader, "readinto", None)
    if readinto is not None:
        return cast("int", readinto(buffer) or 0)
    data = reader.read(len(buffer))
    buffer[: len(data)] = data
    return len(data)


def chunks(reader: Reader, chunk_size: int) -> Iterator[memoryview]:
    """The single-threaded form of :class:`ReadAhead`: one buffer, reused.

    Each view is overwritten by the next read, so the consumer must finish
    with it before asking for another - which :func:`pump` does.
    """
    buffer = bytearray(chunk_size)
    view = memoryview(buffer)
    while count := fill(reader, buffer):
        yield view[:count]


class ReadAhead:
    """One reader, a bounded buffer window, and exception-safe worker shutdown.

    Leaving the block - normally, or with the consumer's exception on its way
    out - stops the worker. Its exit is bounded: a worker blocked in a network
    read that will not return before the stall deadline (half an hour) would
    otherwise hold the caller's ``with`` for that long. So the worker is given
    :data:`STOP_GRACE` to notice; then the source is aborted - ``abort()`` if
    it has one, else ``close()``, called on a helper thread since closing can
    itself block on the connection the worker is stuck on - and the worker is
    waited for :data:`STOP_TIMEOUT` more. One still running after that is
    logged and left behind: it is a daemon, and it stops at its next read.
    """

    def __init__(
        self,
        reader: Reader,
        chunk_size: int,
        depth: int,
        *,
        recycle: bool = True,
        thread_name: str = "xrd-readahead",
    ) -> None:
        """Prepare ``max(depth, 2)`` buffers and the worker; nothing starts until ``with``.

        At least two, so the worker can fill one while the consumer writes
        the other - one would serialise them and gain nothing.
        """
        self._reader = reader
        self._chunk_size = chunk_size
        self._recycle = recycle
        self._buffers: queue.Queue[bytearray] = queue.Queue()
        for _ in range(max(depth, 2)):
            self._buffers.put(bytearray(chunk_size))
        self._ready: queue.Queue[tuple[bytearray, int] | BaseException | None] = queue.Queue()
        self._stop = threading.Event()
        # The worker reads in the caller's context, so the caller's deadline,
        # configuration override and cancel scope bind its reads as they
        # bind the caller's own: a cancelled copy's read-ahead stops too.
        context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=context.run, args=(self._read,), name=thread_name, daemon=True
        )

    def __enter__(self) -> Iterator[memoryview]:
        """Start the worker; the chunks, in order, as views valid until the next one."""
        self._thread.start()
        return self._drain()

    def __exit__(self, *exc: object) -> None:
        """Stop the worker, waiting a bounded time (see the class docstring)."""
        self._stop.set()
        self._buffers.put(bytearray())  # wake a worker waiting for a free buffer
        self._thread.join(STOP_GRACE)
        if not self._thread.is_alive():
            return
        _abort(self._reader, self._thread.name)
        self._thread.join(STOP_TIMEOUT)
        if self._thread.is_alive():
            _log.warning(
                "read-ahead worker %s did not stop within %.0fs of its transfer ending; "
                "leaving it to finish its read",
                self._thread.name,
                STOP_GRACE + STOP_TIMEOUT,
            )

    def _read(self) -> None:
        """The worker: fill free buffers until end of data, a stop, or an error.

        An error is queued as a value so it is raised on the consumer's
        thread, where the caller can see it; ``None`` always follows as the
        end marker, so the consumer never waits on a worker that has died.
        """
        try:
            while not self._stop.is_set():
                buffer = self._buffers.get()
                if self._stop.is_set():
                    break
                count = fill(self._reader, buffer)
                if count <= 0:
                    break
                self._ready.put((buffer, count))
        except BaseException as exc:
            self._ready.put(exc)
        finally:
            self._ready.put(None)

    def _drain(self) -> Iterator[memoryview]:
        """The consumer side: yield each filled buffer, then hand it back.

        The buffer goes back only once the consumer asks for the next chunk,
        which is what makes a yielded view safe to use until then.
        """
        while True:
            item = self._ready.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            buffer, count = item
            yield memoryview(buffer)[:count]
            self._buffers.put(buffer if self._recycle else bytearray(self._chunk_size))


def _abort(reader: object, name: str) -> None:
    """Ask ``reader`` to give up the read its worker is blocked in.

    ``abort()`` is the polite form, for a source that has one; ``close()`` is
    the fallback, and for a socket-backed reader it is what makes a blocked
    ``recv`` return. Either runs on a thread of its own, because it may wait
    on the very lock the stuck read holds, and this is the path that exists to
    keep the caller from waiting. ``reader`` is looked at by attribute only,
    so any object will do; one with neither method is left alone.
    """
    stop = getattr(reader, "abort", None) or getattr(reader, "close", None)
    if not callable(stop):
        return

    def quietly() -> None:
        """Run ``stop`` on the helper thread; a failure here is only logged."""
        try:
            stop()
        except Exception:
            _log.debug("aborting the source of %s failed", name, exc_info=True)

    threading.Thread(target=quietly, name=f"{name}-abort", daemon=True).start()


def _short_write() -> Exception:
    """The default error for a writer that accepted nothing (or too much)."""
    return OSError(errno.EIO, "The destination stopped accepting data before the chunk was written")


def write_all(
    writer: Writer, piece: memoryview, short_write: Callable[[], Exception] = _short_write
) -> None:
    """Commit one chunk, including writers that accept only part of a buffer."""
    at = 0
    while at < len(piece):
        count = writer.write(piece[at:])
        if count is None:  # legacy plugin/stream writers return no byte count
            return
        if count <= 0 or count > len(piece) - at:
            raise short_write()
        at += count


def pump(
    reader: Reader,
    writer: Writer,
    total: int | None,
    chunk_size: int,
    progress: Callable[[int, int | None], None] | None,
    digest: Any,
    depth: int = 1,
    *,
    recycle: bool = True,
    thread_name: str = "xrd-readahead",
    short_write: Callable[[], Exception] = _short_write,
) -> int:
    """Copy ``reader`` to ``writer`` in ``chunk_size`` pieces; the bytes copied.

    ``depth > 1`` reads ahead on a worker thread (:class:`ReadAhead`);
    ``depth == 1`` reads and writes alternately on the caller's thread.
    ``digest`` (a :mod:`hashlib`-like object, or ``None``) sees every byte
    in order, so a checksum is computed on the fly with no second pass.
    ``progress`` is called after each chunk with ``(done, total)``.
    ``recycle`` and ``thread_name`` are passed to :class:`ReadAhead`;
    ``short_write`` builds the error for a writer that stops accepting data.
    Nothing is retried: an error from either side propagates as raised.
    """
    source = (
        ReadAhead(reader, chunk_size, depth, recycle=recycle, thread_name=thread_name)
        if depth > 1
        else nullcontext(chunks(reader, chunk_size))
    )
    done = 0
    with source as pieces:
        for piece in pieces:
            write_all(writer, piece, short_write)
            if digest is not None:
                digest.update(piece)
            done += len(piece)
            if progress is not None:
                progress(done, total)
    return done
