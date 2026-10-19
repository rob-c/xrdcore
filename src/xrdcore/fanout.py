"""Run a handful of jobs at once, and stop them all when one fails.

Every client on the stack splits some work across threads - xrdclient the
spans of a bulk download, the spans of a parallel copy and the readers of a
multi-replica fetch; xgfalclient the ranged segments of an HTTP download;
xrdarc the jobs of an ``arcget`` - and each needs the same four things, which
used to come in a different subset each time:

* the jobs not yet started are not started once one has failed
  (``shutdown(cancel_futures=True)``, and a check before each job starts);
* the jobs already running are told to stop, through a
  :class:`threading.Event` they check between pieces of work - a thread
  cannot be interrupted, but a span loop can look up between chunks;
* the caller sees the *first* failure, with its own type, so an
  ``except FileNotFoundError`` around a parallel copy still works - and the
  failures that came after it are not lost but listed in a note on it
  (:meth:`BaseException.add_note`), which is where a traceback shows them;
* each job runs in a copy of the caller's :mod:`contextvars` context, so a
  deadline, a configuration override or a cancel scope set around the call
  reaches the workers doing it.

An :class:`ExceptionGroup` would carry the extra failures more formally, but
every caller has a public exception type it promises (``GError``, an
``OSError`` subclass, ``ARCError``), and a group is not one of them.

What a maintainer must not break: the first failure is raised *as itself*
(not wrapped), a :class:`Stopped` is never reported as a failure of its own,
the same exception object raised by several jobs is reported once, and the
call returns only after every worker it started has finished - nothing it
started outlives it.

Not every pool is a fan-out. A pool that lives as long as the process - the
one an asyncio facade runs blocking calls on, the one pyxrootd-style
callbacks are dispatched from, a job backend's own thread, the executor a
dask cluster is handed - has none of :func:`run_all`'s questions to answer;
what it needs is to be made once, on first use, and made again in a forked
child, whose copy of the parent's pool has no threads behind it. That is
:class:`Pool`. Between them, :func:`run_all` and :class:`Pool` are where the
chain makes its thread pools: the clients' lint bans
:class:`concurrent.futures.ThreadPoolExecutor` and names these two instead,
so that a hand-rolled fan-out - and the first-failure handling it forgets -
cannot come back unnoticed.
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from typing import ParamSpec, TypeVar

__all__ = ["Pool", "Stopped", "run_all"]

T = TypeVar("T")
P = ParamSpec("P")


class Stopped(Exception):
    """What a job raises when it notices the stop flag and gives up.

    Not a failure of its own - it is the echo of somebody else's - so
    :func:`run_all` leaves it out of the failures it reports, and raises it
    only if nothing else went wrong (a stop the caller asked for).
    """


def _distinct(failures: Sequence[BaseException]) -> list[BaseException]:
    """``failures`` with repeats of one exception object dropped, first sighting kept.

    Several jobs can raise the very same object - a progress callback's
    stored error re-raised by every job's ``check()`` - and that is one
    failure, not one per job; it must not end up in a note on itself.
    """
    seen: list[BaseException] = []
    for failure in failures:
        if not any(failure is known for known in seen):
            seen.append(failure)
    return seen


def _noted(failures: Sequence[BaseException]) -> BaseException:
    """The first of ``failures``, with a note naming every later, distinct one."""
    first, *rest = _distinct(failures)
    if rest:
        listed = "; ".join(f"{type(exc).__name__}: {exc}" for exc in rest)
        first.add_note(f"{len(rest)} more worker(s) failed after this one: {listed}")
    return first


class _Outcome:
    """What the jobs of one :func:`run_all` did wrong, collected as they did it."""

    def __init__(self, halt: threading.Event, on_failure: Callable[[], None] | None) -> None:
        """``halt`` is shared with every job; ``on_failure`` runs on each failing thread.

        ``failures`` and ``echoes`` are appended to from worker threads under
        ``_lock``; they are only read after the pool has shut down, when no
        writer is left.
        """
        self.halt = halt
        self.on_failure = on_failure
        self.failures: list[BaseException] = []
        self.echoes: list[Stopped] = []
        self._lock = threading.Lock()

    def guard(self, call: Callable[[], T]) -> T:
        """Run ``call``, recording - and on a real failure, broadcasting - what it raised.

        A call whose turn comes after the stop is not started at all: the
        pool's own cancellation only reaches calls still queued when the
        failure is noticed, and a free worker can pick one up before that.
        ``BaseException`` is caught on purpose: a ``KeyboardInterrupt`` raised
        inside a job (by a progress callback) must stop the others too.
        """
        try:
            if self.halt.is_set():
                raise Stopped("not started: another call failed first")
            return call()
        except Stopped as exc:
            with self._lock:
                self.echoes.append(exc)
            raise
        except BaseException as exc:
            with self._lock:
                self.failures.append(exc)
            self.halt.set()
            if self.on_failure is not None:
                self.on_failure()
            raise

    def wind_down(self, pool: ThreadPoolExecutor, futures: list[Future[T]]) -> None:
        """Stop what should stop, then wait for what is running.

        Reached with a failure, or with the calling thread interrupted while
        it waited: either way nothing queued should start, and whatever is
        running is asked to stop before it is waited for. Waiting costs
        nothing a caller could have saved by abandoning the pool instead:
        :mod:`concurrent.futures` joins its worker threads at interpreter exit
        anyway, so an abandoned worker only delays the exit to after the
        caller has already reported.
        """
        if self.failures or not all(future.done() for future in futures):
            self.halt.set()
        pool.shutdown(wait=True, cancel_futures=self.halt.is_set())

    def verdict(self) -> None:
        """Raise the first failure, noting the rest; else a stop, if one was asked for."""
        if self.failures:
            raise _noted(self.failures)
        if self.echoes:
            raise self.echoes[0]


def run_all(
    calls: Sequence[Callable[[], T]],
    *,
    workers: int | None = None,
    name: str = "xrd-fanout",
    stop: threading.Event | None = None,
    on_failure: Callable[[], None] | None = None,
) -> list[T]:
    """Run ``calls`` on a thread pool; their results, in order, or the first failure.

    ``workers`` bounds how many run at once (all of them by default); the
    rest wait in the pool's queue and start, in order, as workers free up.
    ``name`` prefixes the worker threads' names, for a traceback or a
    debugger. ``stop`` is set the moment any call raises, for the others to
    notice; pass the event the calls themselves check. ``on_failure`` runs
    then too, on the failing thread, for a caller whose workers wait on each
    other - an ordered writer's turn that will now never come - and need
    waking rather than only telling.

    A single call runs on the calling thread: a pool of one is a thread
    switch and nothing else.
    """
    if len(calls) <= 1:
        return [call() for call in calls]
    outcome = _Outcome(stop if stop is not None else threading.Event(), on_failure)
    pool = ThreadPoolExecutor(max(1, workers or len(calls)), thread_name_prefix=name)
    futures: list[Future[T]] = []
    try:
        futures.extend(
            pool.submit(contextvars.copy_context().run, outcome.guard, call) for call in calls
        )
        wait(futures, return_when=FIRST_EXCEPTION)
    finally:
        outcome.wind_down(pool, futures)
    outcome.verdict()
    return [future.result() for future in futures]


class Pool:
    """A long-lived, named thread pool, made on first use and remade after a fork.

    For work that is not a fan-out: calls submitted one at a time, over the
    life of the process, by code that has its own answer to failure (an
    awaiting coroutine, a callback's handler, a backend's caller). The pool
    adds three things to :class:`~concurrent.futures.ThreadPoolExecutor`:

    * it is not made until something is submitted, so importing the module
      that owns it starts no threads - and ``workers`` may be a callable,
      read then, for a size that comes from configuration;
    * a forked child gets a pool of its own: the parent's executor came
      across the fork without its threads, and work submitted to it there
      would queue forever;
    * the threads carry ``name``, for a traceback or a debugger.

    Calls run in the worker's own :mod:`contextvars` context, as
    :class:`~concurrent.futures.ThreadPoolExecutor` runs them; a caller that
    wants its context to travel submits ``contextvars.copy_context().run``.
    Thread-safe: two threads asking for the executor at once get the same one.
    """

    def __init__(self, name: str, workers: int | Callable[[], int]) -> None:
        """A pool of ``workers`` threads (or as many as ``workers()`` says then) named ``name``."""
        self.name = name
        self._workers = workers
        self._made: tuple[int, ThreadPoolExecutor] | None = None
        self._lock = threading.Lock()

    def executor(self) -> ThreadPoolExecutor:
        """The executor for this process, made now if this process has none yet.

        For an API that takes an executor rather than calls, such as
        :meth:`asyncio.loop.run_in_executor` or a dask ``LocalCluster``.
        The fast path reads without the lock: a stale read can only see an
        older executor (or none) and then takes the lock to check again.
        """
        pid = os.getpid()
        made = self._made
        if made is not None and made[0] == pid:
            return made[1]
        with self._lock:
            if self._made is None or self._made[0] != pid:
                size = self._workers() if callable(self._workers) else self._workers
                self._made = (pid, ThreadPoolExecutor(max(1, size), thread_name_prefix=self.name))
            return self._made[1]

    def submit(self, call: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        """Run ``call(*args, **kwargs)`` on the pool; its future."""
        return self.executor().submit(call, *args, **kwargs)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        """Shut this process's executor down, if it has one; the next submit makes a new one.

        For a test, or an owner that is closing; the arguments are
        :meth:`~concurrent.futures.Executor.shutdown`'s.
        """
        with self._lock:
            made, self._made = self._made, None
        if made is not None and made[0] == os.getpid():
            made[1].shutdown(wait=wait, cancel_futures=cancel_futures)
