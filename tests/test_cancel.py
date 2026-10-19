"""Cancel scopes and cancel hooks."""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from xrdcore import cancel


def test_no_scope_means_never_cancelled_and_unbounded_waits():
    assert cancel.current() is None
    assert not cancel.cancelled()
    cancel.check()
    assert cancel.bounded(None) is None
    assert cancel.bounded(30.0) == 30.0


def test_a_scope_is_level_triggered():
    scope = cancel.CancelScope()

    def body():
        assert cancel.current() is scope
        assert cancel.bounded(None) == cancel.POLL
        assert cancel.bounded(0.01) == 0.01
        scope.cancel()
        for _ in range(2):
            with pytest.raises(cancel.Cancelled, match="the copy was cancelled"):
                cancel.check("the copy")
        return "done"

    assert cancel.within(scope, body) == "done"
    assert cancel.current() is None
    assert repr(scope) == "CancelScope(cancelled)"


def test_nested_scopes_see_an_outer_cancellation():
    outer = cancel.CancelScope()
    inner = cancel.CancelScope(outer)
    assert repr(inner) == "CancelScope(live)"
    outer.cancel()
    assert inner.cancelled


def test_cancelled_escapes_except_exception():
    assert not issubclass(cancel.Cancelled, Exception)


def test_scope_reaches_a_thread_started_with_a_copied_context():
    scope = cancel.CancelScope()
    seen = []

    def body():
        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(lambda: seen.append(cancel.current()),))
        worker.start()
        worker.join()

    cancel.within(scope, body)
    assert seen == [scope]


def test_as_check():
    assert cancel.as_check(None) is None
    event = threading.Event()
    check = cancel.as_check(event)
    assert check is not None and not check()
    event.set()
    assert check()
    hook = cancel.as_check(lambda: True)
    assert hook is not None and hook() is True


def test_watching_fires_once_and_stops_with_the_block():
    fired = []
    with cancel.watching(lambda: True, lambda: fired.append(1), interval=0.001):
        deadline = time.monotonic() + 5
        while not fired and time.monotonic() < deadline:
            time.sleep(0.001)
    assert fired == [1]


def test_watching_treats_a_raising_hook_as_a_cancel():
    fired = threading.Event()

    def broken() -> bool:
        raise RuntimeError("cannot say")

    with cancel.watching(broken, fired.set, interval=0.001):
        assert fired.wait(5)


def test_watching_without_a_hook_runs_no_thread():
    before = threading.active_count()
    with cancel.watching(None, lambda: None):
        assert threading.active_count() == before


def test_watching_never_fires_after_the_block():
    fired = []
    with cancel.watching(lambda: False, lambda: fired.append(1), interval=0.001):
        time.sleep(0.01)
    assert fired == []
