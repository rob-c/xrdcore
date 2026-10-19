"""The one backoff every retry loop waits with: exponential, capped, jittered."""

from __future__ import annotations

import datetime
import email.message
import random
import time

import pytest

from xrdcore import retry as _retry


class _Ceiling:
    """A jitter source that always picks the top of the window."""

    @staticmethod
    def uniform(a: float, b: float) -> float:
        return b


@pytest.fixture(autouse=True)
def _no_jitter():
    """Make every wait exactly its exponential ceiling, as xrdclient's suite does."""
    previous = _retry.use_rng(_Ceiling())
    yield
    _retry.use_rng(previous)


def test_the_ceiling_doubles_from_the_base_and_stops_at_the_cap():
    assert [_retry.ceiling(n, 0.5, 3.0) for n in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]


@pytest.mark.parametrize(("attempt", "base"), [(0, 1.0), (3, 0.0), (2, -1.0)])
def test_no_base_or_no_attempt_means_no_wait(attempt, base):
    assert _retry.ceiling(attempt, base, 10.0) == 0.0
    assert _retry.delay(attempt, base, 10.0) == 0.0


def test_a_thousandth_attempt_neither_overflows_nor_exceeds_the_cap():
    assert _retry.ceiling(1000, 0.5, 600.0) == 600.0


def test_the_wait_is_drawn_from_the_whole_window():
    """Full jitter: anywhere from zero to the ceiling, not a fixed fraction of it."""
    rng = random.Random(1234)
    waits = [_retry.delay(4, 1.0, 100.0, rng=rng) for _ in range(500)]
    assert all(0.0 <= wait <= 8.0 for wait in waits)
    assert min(waits) < 1.0 and max(waits) > 7.0


def test_a_seeded_source_makes_the_waits_repeatable():
    first = [_retry.delay(3, 1.0, 10.0, rng=random.Random(7)) for _ in range(3)]
    again = [_retry.delay(3, 1.0, 10.0, rng=random.Random(7)) for _ in range(3)]
    assert first == again


def test_the_wait_never_outlasts_the_deadline():
    assert _retry.delay(10, 1.0, 600.0, remaining=0.25) == 0.25
    assert _retry.delay(10, 1.0, 600.0, remaining=-3.0) == 0.0


def test_pause_sleeps_for_the_delay_it_returns():
    slept: list[float] = []
    wait = _retry.pause(2, 1.0, 10.0, sleep=slept.append)
    assert slept == [wait] == [2.0]  # the suite pins jitter to the ceiling


def test_pause_does_not_sleep_at_all_for_a_zero_wait(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("slept"))
    assert _retry.pause(1, 0.0, 10.0) == 0.0


def test_pause_uses_time_sleep_as_it_is_when_called(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _retry.pause(1, 0.5, 10.0)
    assert slept == [0.5]


def test_use_rng_swaps_the_source_and_hands_back_the_old_one():
    seeded = random.Random(3)
    previous = _retry.use_rng(seeded)
    try:
        expected = random.Random(3).uniform(0.0, 4.0)
        assert _retry.delay(3, 1.0, 10.0) == expected
    finally:
        assert _retry.use_rng(previous) is seeded


# -- Retry-After (ported from xrdclient's tests/test_public_modules.py) ----------

NOW = datetime.datetime(2026, 10, 21, 7, 28, 0, tzinfo=datetime.UTC).timestamp()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        (" 0 ", 0.0),
        ("Wed, 21 Oct 2026 07:30:00 GMT", 120.0),  # IMF-fixdate
        ("Wednesday, 21-Oct-26 07:29:00 GMT", 60.0),  # obsolete RFC 850
        ("Wed Oct 21 07:28:30 2026", 30.0),  # obsolete asctime, no zone: UTC
        ("Wed, 21 Oct 2026 07:00:00 GMT", 0.0),  # already past: wait no longer
        ("-5", None),
        ("1.5", None),
        ("\u0663", None),  # ARABIC-INDIC DIGIT THREE is not a DIGIT
        ("", None),
        ("soon", None),
    ],
)
def test_retry_after_reads_both_forms(value, expected):
    assert _retry.retry_after({"Retry-After": value}, now=NOW) == expected


def test_retry_after_ignores_the_case_of_the_field_name():
    assert _retry.retry_after({"retry-after": "7"}) == 7.0
    assert _retry.retry_after({"RETRY-AFTER": "8"}) == 8.0
    message = email.message.Message()  # what http.client.HTTPResponse.headers is
    message["Retry-After"] = "9"
    assert _retry.retry_after(message) == 9.0


def test_retry_after_without_the_header_is_none():
    assert _retry.retry_after({}) is None
    assert _retry.retry_after(email.message.Message()) is None


def test_retry_after_measures_a_date_from_the_clock_by_default():
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    header = later.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 3500 < (_retry.retry_after({"Retry-After": header}) or 0) <= 3600
