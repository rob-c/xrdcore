"""The one backoff every retry loop on the stack waits with.

Exponential, capped, deadline-aware and with *full jitter*: the n-th wait is
drawn uniformly from ``[0, min(cap, base * 2**(n-1))]``, as the AWS
architecture blog's "Exponential Backoff And Jitter" recommends. The jitter is
the point. Without it every client that lost the same data server at the
same moment - a farm of batch jobs, a ``DataLoader`` with sixteen workers -
comes back at the same moment too, on every retry, and the server restarting
under them sees one synchronised stampede after another. Spreading each wait
over its whole window costs nothing on average and breaks the lockstep.

The random source is module state so that a test can pin it: :func:`use_rng`
swaps in anything with a ``uniform(a, b)`` - a seeded :class:`random.Random`,
or an object that always answers ``b`` to make every wait its ceiling, which
is what the suite does to keep the timing it asserts on exact.

A server can also say how long to wait: HTTP's ``Retry-After`` on a ``429``
or ``503`` (RFC 9110 section 10.2.3). :func:`retry_after` reads it, in both
of its spellings, so that every HTTP client on the stack - xrdclient's,
xgfalclient's plug-ins, xrdarc's A-REX session - honours it the same way. It only reads;
whether to wait that long, and for how many attempts, stays the caller's
policy (cap it with the same ``cap`` :func:`delay` takes: a server asking
for an hour is not a reason to hang a batch job for one).

The jitter source is shared by every client that waits through this module,
so a test that swaps it with :func:`use_rng` pins every loop at once.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from email.message import Message

__all__ = ["Uniform", "ceiling", "delay", "pause", "retry_after", "use_rng"]


class Uniform(Protocol):
    """What a random source must offer: :meth:`random.Random.uniform`."""

    def uniform(self, a: float, b: float) -> float:
        """A float drawn uniformly from ``[a, b]``."""


#: Where the jitter comes from. Not a cryptographic source, and it does not
#: need to be: all it has to do is not agree with the next client's.
_rng: Uniform = random.Random()


def use_rng(rng: Uniform) -> Uniform:
    """Draw jitter from ``rng`` from now on; returns the source it replaces."""
    global _rng
    previous, _rng = _rng, rng
    return previous


def ceiling(attempt: int, base: float, cap: float) -> float:
    """The longest the ``attempt``-th wait may be: ``base`` doubled, capped.

    ``attempt`` counts from one. The exponent is clamped before it is raised,
    so the thousandth retry of a long-lived loop neither overflows a float nor
    spends time computing a number the cap throws away.
    """
    if base <= 0 or attempt <= 0:
        return 0.0
    return min(base * 2.0 ** min(attempt - 1, 62), cap)


def delay(
    attempt: int,
    base: float,
    cap: float,
    *,
    remaining: float | None = None,
    rng: Uniform | None = None,
) -> float:
    """How long to wait before retry number ``attempt`` (from one).

    Zero when ``base`` is zero - a caller that configured no backoff gets
    none, jitter or not. ``remaining`` is what is left of a deadline, and the
    wait never runs past it: sleeping beyond the point where the operation has
    expired only delays the error the caller is about to get.
    """
    top = ceiling(attempt, base, cap)
    wait = (rng or _rng).uniform(0.0, top) if top > 0 else 0.0
    if remaining is not None:
        wait = min(wait, max(remaining, 0.0))
    return wait


def pause(
    attempt: int,
    base: float,
    cap: float,
    *,
    remaining: float | None = None,
    sleep: Callable[[float], object] | None = None,
) -> float:
    """Sleep for :func:`delay`'s answer, and return it.

    No sleep at all when the answer is zero, so a loop configured without a
    backoff does not even yield. ``sleep`` defaults to :func:`time.sleep` as
    it is when called, which is what lets a test patch it. The jitter source
    is always the module's shared generator: no caller of the sleeping form
    needs a seeded one (tests that pin jitter call :func:`delay` directly).
    """
    wait = delay(attempt, base, cap, remaining=remaining)
    if wait > 0:
        (sleep or time.sleep)(wait)
    return wait


def retry_after(headers: Mapping[str, str] | Message, *, now: float | None = None) -> float | None:
    """The seconds a response's ``Retry-After`` header asks to wait, or ``None``.

    RFC 9110 section 10.2.3 allows two forms, and both are read:

    * **delay-seconds** - one or more ASCII digits, a relative wait
      (``Retry-After: 120``);
    * **HTTP-date** - an absolute time (``Retry-After: Wed, 21 Oct 2026
      07:28:00 GMT``), turned into seconds from ``now`` (default
      :func:`time.time`, a parameter so a caller with its own clock - or a
      test - need not patch one). A date already past answers ``0.0``:
      the server's wait is over, not invalid.

    ``headers`` is anything with the header names as keys -
    :class:`http.client.HTTPMessage` (an :class:`email.message.Message`,
    looked up case-insensitively by its own ``get``), urllib3's header dict,
    or a plain ``dict``, which is searched case-insensitively here because
    HTTP field names are (RFC 9110 section 5.1). ``None`` means no usable
    answer - absent, empty, negative, fractional or unparsable - and the
    caller falls back on its own backoff; a malformed header is a server bug
    that must not turn into an exception on the retry path.
    """
    value = _field(headers, "retry-after")
    if value is None:
        return None
    text = value.strip()
    # ``isdigit`` alone accepts other scripts' digits ("\u0663"), which the
    # grammar's DIGIT (RFC 5234: %x30-39) does not.
    if text.isascii() and text.isdigit():
        return float(int(text))
    return _until(text, time.time() if now is None else now)


def _field(headers: Mapping[str, str] | Message, name: str) -> str | None:
    """Header ``name`` from ``headers``, ignoring case whatever the container."""
    found = headers.get(name)
    if found is not None:
        return str(found)
    # A plain dict is case-sensitive; HTTP field names are not. A
    # ``Message`` already answered above if it had the field at all.
    return next((str(v) for k, v in headers.items() if str(k).lower() == name), None)


def _until(text: str, now: float) -> float | None:
    """Seconds from ``now`` to the HTTP-date ``text``; ``None`` if it is not one.

    :mod:`email.utils` is imported here, not at the top: this module is on
    every retry loop's import path and the date form is rare.
    """
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    # IMF-fixdate is always GMT; ``parsedate_to_datetime`` leaves "-0000"
    # (and the obsolete forms without a zone) naive, which here means UTC.
    if when.tzinfo is None:
        import datetime

        when = when.replace(tzinfo=datetime.UTC)
    return max(0.0, when.timestamp() - now)
