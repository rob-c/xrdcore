"""Credential redaction for logs and terminals, and per-level muting.

Every logger :func:`get_logger` hands out carries one shared filter that
redacts credential material, so enabling DEBUG never leaks a token into a log
file. The filter is added to each logger rather than to a hierarchy's root
because :mod:`logging` runs a logger's filters only for records logged
*through that logger*, never for its children's; a client keeps its own
naming (``xrdclient.session``, ``gfal2``) and asks for its loggers here.

:func:`redact` is the same rewriting as a plain function, for the paths that
reach a terminal without going through :mod:`logging` - an error message a
CLI prints, the ``message`` of a report record (:mod:`xrdcore.report`).

What a maintainer must not break: redaction runs on the *formatted* message
(see :class:`RedactingFilter`), and :data:`_PATTERNS` only ever grows - a
pattern removed is a credential that starts reaching log files.

The same filter can mute parts of a hierarchy at some levels and not others
(:func:`mute`) - what XrdCl's per-level topic masks do, and what a
``SetLogMask`` compatibility call is built on.

:func:`configure` is the other end: what a command-line tool does with its
``-v`` count (or ``-d LEVEL``) - one handler, on the loggers the tool names,
writing through the same redaction. xrdarc, xrdwms, xrdclient's ``xrd-*``
and xgfalclient's ``gfal-*`` each used to build that handler themselves;
they now pick only what is visibly theirs (the scale of :func:`verbosity_level`,
the loggers, the stream, the line format).
"""

from __future__ import annotations

import dataclasses
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Protocol, TextIO

__all__ = [
    "VERBOSITY",
    "Configured",
    "RedactingFilter",
    "configure",
    "get_logger",
    "mute",
    "redact",
    "verbosity_level",
]

#: Query parameters (and ``key=value`` fields of an ``Authorization``
#: header) whose value is a credential or a signature that works as one:
#: S3 pre-signed URLs (``X-Amz-Signature``, ``X-Amz-Credential``,
#: ``X-Amz-Security-Token``, the older ``Signature`` and ``AWSAccessKeyId``),
#: Azure and Google ``sig``, EOS capabilities (``cap.sym``, ``cap.msg``), and
#: OAuth's ``access_token``. The look-behind keeps ``sig=`` from matching the
#: end of some longer name; ``,`` ends a value inside a SigV4 header.
_SECRET_PARAMS = (
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "awsaccesskeyid",
    "signature",
    "credential",
    "sig",
    "cap\\.sym",
    "cap\\.msg",
    "access_token",
    "authz",
)

_PATTERNS = (
    re.compile(rf"((?<![A-Za-z0-9_.])(?:{'|'.join(_SECRET_PARAMS)})=)[^&\s'\",]+", re.I),
    # A whole ``Authorization``/``Proxy-Authorization``/``TransferHeaderAuthorization``
    # value, header or dict entry: the scheme is kept, everything after it goes -
    # ``AWS4-HMAC-SHA256 Credential=..., SignedHeaders=..., Signature=...`` included.
    re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?(?:[A-Za-z0-9-]+\s+)?)[^'\"\r\n]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"(eyJ[A-Za-z0-9_-]{4,})\.[A-Za-z0-9._-]+"),
    # A macaroon: its base64 always opens ``MDA`` (``"00"``, the first packet's
    # length), wherever it turns up - a header, an ``authz`` CGI, a JSON reply.
    re.compile(r"(MDA[A-Za-z0-9])[A-Za-z0-9+/_-]{16,}={0,2}"),
    re.compile(r"((?:token|password|secret|keytab|cred)['\"]?\s*[=:]\s*)\S+", re.I),
)


def redact(text: str) -> str:
    """Replace credential material in ``text`` with ``<redacted>``."""
    for pat in _PATTERNS:
        text = pat.sub(r"\1<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    """A :class:`logging.Filter` that interpolates a record, then redacts the result.

    Attach one to any logger or handler whose records may carry a URL, a
    header or a token - every logger from :func:`get_logger` already has the
    shared :data:`FILTER`. It also applies the :func:`mute` bands. Instances
    hold no state of their own: any number may be made, and one may be
    shared between threads.

    Redacting the format string and the arguments separately misses anything
    that only looks like a credential once they are joined - ``"%s=%s"`` with
    ``("token", secret)`` sails straight through - and it can corrupt the
    format string itself, since ``"keytab: %s"`` is exactly the shape of a
    secret assignment. Formatting up front has neither problem, and handlers
    call :meth:`~logging.LogRecord.getMessage` anyway.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop a muted record; otherwise rewrite it to its redacted message.

        Always ``True`` for an unmuted record: this filter cleans, it never
        hides. ``args`` is emptied whenever the message is replaced so a
        handler does not interpolate the already-formatted text a second time.
        """
        if _muted and _is_muted(record):
            return False
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True  # a broken format string is logging's problem, not ours
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = ()
        return True


#: ``(lowest level, level above the highest) -> logger prefixes`` whose
#: records in that band of levels are dropped. Replaced, never mutated, so
#: the filter reads it without a lock.
_muted: dict[tuple[int, int], tuple[str, ...]] = {}


def mute(low: int, high: int, loggers: Iterable[str]) -> None:
    """Drop records from ``loggers`` with a level from ``low`` up to ``high``.

    Each name covers its logger and every logger below it, and replaces what
    was muted in that band before; no names un-mutes it.
    """
    global _muted
    prefixes = tuple(f"{name}." for name in loggers)
    changed = {band: names for band, names in _muted.items() if band != (low, high)}
    if prefixes:
        changed[(low, high)] = prefixes
    _muted = changed


def _is_muted(record: logging.LogRecord) -> bool:
    """Whether a :func:`mute` band covers this record's level and logger.

    The trailing dot makes ``"xrdclient.http"`` match ``xrdclient.http`` and
    its children but not ``xrdclient.httpx``.
    """
    name = f"{record.name}."
    return any(
        low <= record.levelno < high and name.startswith(prefixes)
        for (low, high), prefixes in _muted.items()
    )


#: The one filter every logger from :func:`get_logger` carries; muting state
#: is module-wide, so a :func:`mute` reaches every client's loggers at once.
FILTER = RedactingFilter()


def get_logger(name: str) -> logging.Logger:
    """The logger called ``name``, with redaction (and muting) applied.

    Idempotent: :meth:`logging.Filterer.addFilter` ignores a filter the
    logger already has, so asking twice never redacts twice.
    """
    log = logging.getLogger(name)
    log.addFilter(FILTER)
    return log


# -- a command-line tool's logging --------------------------------------------------------

#: ``-v`` counted: none, ``-v`` and ``-vv`` (more is the same as ``-vv``) - the
#: scale of xrdarc and xrdwms. A tool with another scale passes its own to
#: :func:`verbosity_level` (gfal2-util starts at ERROR and takes three steps).
VERBOSITY = (logging.WARNING, logging.INFO, logging.DEBUG)

#: The line :func:`configure` writes unless told otherwise: ``WARNING: text``.
_FORMAT = "%(levelname)s: %(message)s"


def verbosity_level(count: int, levels: Sequence[int] = VERBOSITY) -> int:
    """The logging level ``count`` repetitions of ``-v`` ask for on the scale ``levels``.

    Past the end of the scale is its last level (``-vvvv`` is no louder than
    ``-vv`` on the default one); a negative count is its first.
    """
    return levels[min(max(count, 0), len(levels) - 1)]


class _Writable(Protocol):
    """What :func:`configure`'s handler writes to: a text stream, as far as logging uses one."""

    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


class _Stderr:
    """Whatever ``sys.stderr`` is when a record is logged, not when logging was set up.

    A tool's ``main`` may run many times in one process (every test suite
    does), and a ``--json``/``--xml`` run swaps ``sys.stderr`` for its report
    (:mod:`xrdcore.report`): a handler holding the stream it was made with
    would write into a finished report, or a closed stream, afterwards.
    """

    def write(self, text: str) -> int:
        """Write to the current ``sys.stderr``."""
        return sys.stderr.write(text)

    def flush(self) -> None:
        """Flush the current ``sys.stderr``."""
        sys.stderr.flush()


class _Handler(logging.StreamHandler[_Writable]):
    """The handler :func:`configure` installs: a class of its own, so the next call can find it.

    Configuring twice (``main`` run again in one process) replaces it rather
    than adding a second one, which would print every line twice.
    """


@dataclasses.dataclass(frozen=True)
class Configured:
    """What one :func:`configure` call changed, so :meth:`close` can put it back.

    A tool that owns its process can ignore it - the process ends. One whose
    ``main`` may run inside another program (gfal2-util's commands are
    called from wrappers and tests) closes it when the command ends.
    """

    #: The one handler installed on every named logger.
    handler: logging.Handler
    #: Each named logger with the level it had before.
    previous: tuple[tuple[logging.Logger, int], ...]

    def close(self) -> None:
        """Remove the handler, restore each logger's level, and close the handler.

        The stream is not closed: it belongs to whoever passed it (a log
        file the tool opened, ``sys.stdout``). Closing two results out of
        order restores the older levels last, as a stack would.
        """
        for logger, level in self.previous:
            logger.removeHandler(self.handler)
            logger.setLevel(level)
        self.handler.close()


def configure(
    level: int,
    *,
    loggers: Iterable[str] = ("",),
    stream: TextIO | None = None,
    fmt: str | logging.Formatter = _FORMAT,
) -> Configured:
    """Send records at ``level`` and above from ``loggers`` to ``stream``, redacted.

    ``level`` is usually :func:`verbosity_level` of the tool's ``-v`` count,
    or the level a ``-d LEVEL`` option names. Each logger in ``loggers``
    (``""`` is the root, which sees every library's records) gets that level
    and one shared handler; the handler has the level too, so a child logger
    set louder by someone else does not get past it. ``stream`` defaults to
    whatever ``sys.stderr`` is at each record (see :class:`_Stderr`); ``fmt``
    is a ``%``-style format or a ready :class:`logging.Formatter` (a tool
    that colours level names on a terminal brings its own).

    Every record the handler writes passes :data:`FILTER` first - the same
    redaction and :func:`mute` bands as a :func:`get_logger` logger - so a
    third-party library logging a pre-signed URL through the root does not
    put the signature on the terminal either.

    A handler an earlier call installed on one of these loggers is removed
    first, so configuring again changes the level and stream instead of
    duplicating every line. The result undoes this call (:class:`Configured`).
    """
    handler = _Handler(_Stderr() if stream is None else stream)
    handler.setLevel(level)
    handler.setFormatter(fmt if isinstance(fmt, logging.Formatter) else logging.Formatter(fmt))
    handler.addFilter(FILTER)
    previous: list[tuple[logging.Logger, int]] = []
    for name in loggers:
        logger = logging.getLogger(name)
        for earlier in [h for h in logger.handlers if isinstance(h, _Handler)]:
            logger.removeHandler(earlier)
        previous.append((logger, logger.level))
        logger.setLevel(level)
        logger.addHandler(handler)
    return Configured(handler, tuple(previous))
