"""One way to run jobs side by side: first failure wins, the rest stop.

Ported from xrdclient's ``tests/test_fanout.py`` (the module was
``xrdclient._fanout`` until 0.4.0), with the cases xgfalclient's ranged
download and xrdarc's ``get`` relied on when they had their own pools.
"""

from __future__ import annotations

import contextvars
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from xrdcore import fanout


def _square(n: int) -> int:
    return n * n


def test_results_come_back_in_the_order_the_calls_were_given():
    calls = [functools.partial(_square, n) for n in range(6)]
    assert fanout.run_all(calls, workers=3) == [
        0,
        1,
        4,
        9,
        16,
        25,
    ]


def test_a_single_call_runs_on_the_calling_thread():
    seen = []
    fanout.run_all([lambda: seen.append(threading.current_thread())])
    assert seen == [threading.current_thread()]
    assert fanout.run_all([]) == []


def test_the_first_failure_is_raised_with_its_own_type_and_the_rest_noted():
    both = threading.Barrier(2, timeout=5)
    halt = threading.Event()

    def first():
        both.wait()
        raise FileNotFoundError("the first")

    def second():
        both.wait()
        halt.wait(5)  # fails only once the first has, so the order is known
        raise RuntimeError("the second")

    with pytest.raises(FileNotFoundError, match="the first") as caught:
        fanout.run_all([first, second], stop=halt)
    notes = getattr(caught.value, "__notes__", [])
    assert notes and "RuntimeError: the second" in notes[0]


def test_a_failure_stops_the_others_and_calls_on_failure():
    halt = threading.Event()
    woken = []
    started = threading.Event()

    def cooperative():
        started.set()
        while True:
            if halt.is_set():
                raise fanout.Stopped("told to")
            halt.wait(0.01)

    def failing():
        started.wait(5)
        raise ValueError("bad span")

    with pytest.raises(ValueError, match="bad span") as caught:
        fanout.run_all([cooperative, failing], stop=halt, on_failure=lambda: woken.append(1))
    assert halt.is_set() and woken == [1]
    # The echo of the stop is not reported as a failure of its own.
    assert not getattr(caught.value, "__notes__", [])


def test_calls_not_yet_started_are_never_started_after_a_failure():
    ran = []

    def failing():
        raise OSError("no")

    with pytest.raises(OSError):
        fanout.run_all([failing, lambda: ran.append(1), lambda: ran.append(2)], workers=1)
    assert ran == []


def test_a_stop_nobody_failed_for_is_raised_as_the_stop():
    halt = threading.Event()
    halt.set()

    def stopping():
        raise fanout.Stopped("asked")

    with pytest.raises(fanout.Stopped):
        fanout.run_all([stopping, lambda: 1], stop=halt)


def test_each_call_sees_the_callers_context():
    flavour: contextvars.ContextVar[str] = contextvars.ContextVar("flavour", default="plain")
    flavour.set("salted")
    assert fanout.run_all([flavour.get, flavour.get]) == ["salted", "salted"]


def test_an_interrupted_wait_stops_the_workers(monkeypatch):
    halt = threading.Event()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(fanout, "wait", interrupted)

    def patient():
        halt.wait(5)
        return 1

    with pytest.raises(KeyboardInterrupt):
        fanout.run_all([patient, patient], stop=halt)
    assert halt.is_set()


def test_one_exception_raised_by_several_calls_is_reported_once():
    """A shared callback's error re-raised by every job is one failure, not a note on itself."""
    shared = OSError("the callback said no")
    both = threading.Barrier(2, timeout=5)

    def raising():
        both.wait()
        raise shared

    with pytest.raises(OSError) as caught:
        fanout.run_all([raising, raising])
    assert caught.value is shared
    assert not getattr(shared, "__notes__", [])


def test_a_keyboard_interrupt_in_a_call_stops_the_queued_ones_and_propagates():
    """Ctrl-C raised inside a job (by a progress callback) ends the whole fan-out."""
    ran = []
    halt = threading.Event()

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fanout.run_all([interrupted, lambda: ran.append(1)], workers=1, stop=halt)
    assert ran == [] and halt.is_set()


def test_the_worker_threads_carry_the_name_and_are_gone_afterwards():
    names = fanout.run_all(
        [lambda: threading.current_thread().name] * 2, workers=2, name="xrd-test-pool"
    )
    assert all(name.startswith("xrd-test-pool") for name in names)
    assert not [t for t in threading.enumerate() if t.name.startswith("xrd-test-pool")]


# --- Pool: the long-lived pools that are not fan-outs ---------------------------


def test_a_pool_starts_no_thread_until_something_is_submitted():
    pool = fanout.Pool("xrd-test-lazy", 2)
    assert pool._made is None
    try:
        assert pool.submit(_square, 7).result(timeout=10) == 49
        assert (
            pool.submit(lambda: threading.current_thread().name)
            .result(timeout=10)
            .startswith("xrd-test-lazy")
        )
        assert pool.executor() is pool.executor()
    finally:
        pool.shutdown()


def test_a_callable_size_is_read_when_the_pool_is_made_and_never_below_one():
    sizes = iter([0, 5])
    pool = fanout.Pool("xrd-test-size", lambda: next(sizes))
    try:
        assert pool.executor()._max_workers == 1
        pool.shutdown()
        assert pool.executor()._max_workers == 5  # made again: the size is read again
    finally:
        pool.shutdown()


def test_a_forked_child_gets_a_pool_of_its_own(monkeypatch: pytest.MonkeyPatch):
    """The parent's executor has no threads in the child; it is replaced, not reused."""
    pool = fanout.Pool("xrd-test-fork", 1)
    parent = pool.executor()
    try:
        monkeypatch.setattr(os, "getpid", lambda: -1)
        child = pool.executor()
        assert child is not parent and pool.submit(_square, 3).result(timeout=10) == 9
        pool.shutdown()  # the child's own executor
        assert child._shutdown and pool._made is None
    finally:
        monkeypatch.undo()
        parent.shutdown()


def test_shutting_down_in_a_child_leaves_the_parents_executor_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    pool = fanout.Pool("xrd-test-forked-shutdown", 1)
    parent = pool.executor()
    try:
        monkeypatch.setattr(os, "getpid", lambda: -1)
        pool.shutdown()  # not this process's executor: forgotten, not shut down
        assert not parent._shutdown and pool._made is None
        pool.shutdown()  # nothing made: nothing to do
    finally:
        monkeypatch.undo()
        parent.shutdown()


def test_two_threads_asking_at_once_get_one_executor(monkeypatch: pytest.MonkeyPatch):
    """The thread that loses the race for the lock finds the winner's executor there."""
    pool = fanout.Pool("xrd-test-race", 1)
    winner = ThreadPoolExecutor(1)

    class _Racing:
        """A lock whose acquisition finds another thread already made the executor."""

        def __enter__(self) -> None:
            pool._made = (os.getpid(), winner)

        def __exit__(self, *exc: object) -> None:
            """Nothing to release."""

    monkeypatch.setattr(pool, "_lock", _Racing())
    try:
        assert pool.executor() is winner
    finally:
        monkeypatch.undo()
        pool.shutdown()
    assert winner._shutdown
