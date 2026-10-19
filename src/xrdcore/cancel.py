"""Cancellation: how a call learns that whoever started it has given up.

There are two ways in, for two kinds of caller, and they meet here so that a
maintainer has one place to read - and so that every library on the stack
(the XRootD session, the HTTP connections, the copy pipeline) looks at the
*same* scope, whichever client started the call.

* **Scopes** - :class:`CancelScope`, below - are how an asyncio front end
  cancels the worker thread behind a cancelled coroutine. Nothing is torn
  down: the request is abandoned where it waits, and the connection stays in
  use.
* **Hooks** - a ``cancel=`` argument on a long transfer - are how a
  synchronous caller stops it from another thread (gfal2's ``cancel()``, a
  GUI button, a signal handler). A hook is asked between pieces of work, and
  a watcher thread (:func:`watching`) aborts the handles in flight the moment
  it fires, so a worker blocked on the network wakes at once. That costs the
  connections, which is the point: the transfer is over, and so is anything
  it had outstanding.

Scopes
------

A scope reaches a request already waiting on the wire.

A blocking call cannot be interrupted from outside its thread. What can be
done is to tell the worker that nobody is waiting for the answer any more,
and have every place the worker might wait look for that. This module is the
telling::

    scope = CancelScope()
    worker = pool.submit(contextvars.copy_context().run, within, scope, call)
    ...
    scope.cancel()          # from any thread - the event loop's, typically

A :class:`CancelScope` travels in a :mod:`contextvars` variable, so it reaches
whatever the call runs - including work started on threads of its own (a
fan-out, the copy read-ahead in :mod:`xrdcore.pipeline`) that copy the context
they were started from.

Once a scope is cancelled it stays cancelled (*level*-triggered, as Trio's
scopes are, not edge-triggered as :mod:`asyncio`'s are): every wait inside it
raises :class:`Cancelled` from then on, not only the first. That is what makes
a cancelled worker come back promptly whatever it was doing - a copy that
caught the first one in a retry, or a fan-out whose other jobs were mid-read,
would otherwise carry on regardless. The price is that a cancelled call's own
clean-up round trips (a checkpoint rollback, removing a partial remote target)
are skipped too; they would be waits on the very server that was too slow.

What looks at the scope is the transport's business: a session before it
sends a request and while it waits for one, an HTTP connection before it
sends. Waiting is done in slices of :data:`POLL` seconds while a scope is in
force (:func:`bounded`), which is how a waiter notices a cancellation without
anyone having to wake it: the canceller may be an event loop that must never
block on a session's lock, and a socket read cannot be woken without shutting
the socket down.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

__all__ = [
    "POLL",
    "Cancel",
    "CancelScope",
    "Cancelled",
    "as_check",
    "bounded",
    "cancelled",
    "check",
    "current",
    "watching",
    "within",
]

T = TypeVar("T")

#: The longest a wait inside a scope goes without looking at it, in seconds.
#: Short enough that a cancelled worker is back well within half a second,
#: long enough that a parked waiter costs a few wake-ups a second and no more.
POLL = 0.05


class Cancelled(BaseException):
    """The call was cancelled; the request it was waiting on has been abandoned.

    A :class:`BaseException`, like :class:`asyncio.CancelledError` and for the
    same reason: it has to travel out of the worker through every
    ``except Exception`` and every client's own error base on the way - a
    retry, a fall-back to another replica, a re-open after a lost server -
    without any of them taking it for a failure worth recovering from. An
    asyncio caller never sees it: the coroutine that was cancelled has raised
    its own :class:`asyncio.CancelledError` already, and this ends the
    worker's call.
    """


class CancelScope:
    """A flag any thread may set, telling the calls inside the scope to stop.

    ``parent`` is the scope in force where this one was made: a scope nested
    in a cancelled one is cancelled too, so an inner scope never shields a
    call from an outer cancellation.
    """

    __slots__ = ("_event", "_parent")

    def __init__(self, parent: CancelScope | None = None) -> None:
        """A live scope; ``parent`` is normally :func:`current` where it is made."""
        self._event = threading.Event()
        self._parent = parent

    def cancel(self) -> None:
        """Cancel every call inside this scope. Idempotent, and never blocks."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """Whether this scope, or any scope it is nested in, has been cancelled.

        Walks the parent chain on every read instead of copying the parent's
        state in, so cancelling an outer scope reaches inner scopes made
        before it was cancelled. Lock-free: ``Event.is_set`` is thread-safe.
        """
        if self._event.is_set():
            return True
        return self._parent is not None and self._parent.cancelled

    def __repr__(self) -> str:
        """``CancelScope(live)`` or ``CancelScope(cancelled)``, for debugging."""
        return f"CancelScope({'cancelled' if self.cancelled else 'live'})"


#: The innermost scope in force here, or ``None`` outside every one.
_scope: contextvars.ContextVar[CancelScope | None] = contextvars.ContextVar(
    "xrdcore_cancel", default=None
)


def current() -> CancelScope | None:
    """The scope in force in this thread or task, if any."""
    return _scope.get()


def within(scope: CancelScope, call: Callable[[], T]) -> T:
    """Run ``call`` with ``scope`` in force, then put the old one back."""
    token = _scope.set(scope)
    try:
        return call()
    finally:
        _scope.reset(token)


def cancelled() -> bool:
    """Whether the scope in force has been cancelled."""
    scope = _scope.get()
    return scope is not None and scope.cancelled


def check(what: str = "the request") -> None:
    """Raise :class:`Cancelled` if the scope in force has been cancelled."""
    if cancelled():
        raise Cancelled(f"{what} was cancelled")


def bounded(timeout: float | None) -> float | None:
    """``timeout``, cut to :data:`POLL` while a scope is in force.

    For a wait that must notice a cancellation: inside a scope it wakes at
    least that often to look, and outside one it waits as long as it was
    always going to.
    """
    if _scope.get() is None:
        return timeout
    return POLL if timeout is None else min(timeout, POLL)


# ---------------------------------------------------------------------------
# Hooks: ``cancel=`` on a long transfer
# ---------------------------------------------------------------------------

#: What a ``cancel=`` argument may be: a :class:`threading.Event` - set it to
#: cancel - or a callable answering "should this stop now?", which is what
#: gfal2's and XrdCl's progress handlers' ``ShouldCancel`` is. A callable is
#: asked from a worker thread, so it must be thread-safe and quick.
Cancel = Callable[[], bool] | threading.Event


def as_check(cancel: Cancel | None) -> Callable[[], bool] | None:
    """``cancel`` as a callable, or ``None`` for no hook at all."""
    if cancel is None:
        return None
    if isinstance(cancel, threading.Event):
        return cancel.is_set
    return cancel


@contextmanager
def watching(
    check: Callable[[], bool] | None,
    on_cancel: Callable[[], None],
    *,
    interval: float = POLL,
    name: str = "xrd-cancel",
) -> Iterator[None]:
    """While the block runs, call ``on_cancel`` once if ``check`` says to stop.

    ``check`` is asked every ``interval`` seconds on a thread of its own, which
    is what reaches a worker blocked between pieces - asking only between
    them would leave a stalled read to its timeout. ``on_cancel`` runs on that
    thread and should do no more than wake the workers (abort their handles,
    set their stop flag); the watcher is gone by the time the block exits, so
    it never runs after. A hook that raises counts as a cancel: a callback
    that cannot answer is no reason to keep going a transfer nobody can stop.
    """
    if check is None:
        yield
        return
    done = threading.Event()

    def watch() -> None:
        """Poll ``check`` until the block ends or it says stop, then fire once."""
        while not done.wait(interval):
            try:
                stop = check()
            except Exception:
                stop = True
            if stop:
                on_cancel()
                return

    watcher = threading.Thread(target=watch, name=name, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        watcher.join()
