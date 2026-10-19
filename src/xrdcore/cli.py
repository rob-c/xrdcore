"""The exit statuses every command-line tool on the stack shares, and how a run ends.

xrdarc, xrdwms and the storage clients' tools all end a command the same few
ways, and used to spell each one separately:

* :class:`ExitCode` - the statuses whose meaning is the same in every tool:
  0 success, 1 a failure the tool reported, 2 a usage error (argparse's own),
  70 a bug (``EX_SOFTWARE`` from ``sysexits.h``), and the shell's
  ``128 + signal`` for Ctrl-C (130) and a reader that went away (141). A
  tool with codes of its own - xrdarc's 3-7 for credentials, descriptions,
  CEs, local faults and timeouts; xrdwms's for configuration and backends -
  defines its own :class:`enum.IntEnum` that restates these members and adds
  its own, checked by :func:`extends` (an enum with members cannot be
  subclassed, so this is the extension Python allows);
* :func:`run` - the wrapper around a command's body that turns Ctrl-C into
  130 and ``BrokenPipeError`` into a *quiet* 141, and hands every other
  exception to the tool's own reporting;
* :func:`silence_stdout` - what makes that 141 quiet;
* :class:`Fatal` - the usual handler for everything else: one line on
  stderr (``prog: message``, redacted), an ``error`` record in a
  ``--json``/``--xml`` report, and the exit status from a table of
  exception classes the tool supplies (:func:`exit_code`), with the
  traceback of a bug logged at DEBUG rather than printed. xrdarc and
  xrdwms end every command through it; xrdclient's ``fail`` is one.

Tools that mirror an upstream tool's statuses keep them: gfal2-util's
``gfal-*`` exit with an errno (``EINTR`` after Ctrl-C), XrdCl's ``xrd*`` with
theirs, because scripts written against those tools test those numbers.
They use this module only where their statuses coincide (0, 1) and for
:func:`silence_stdout` (and xrdclient for :class:`Fatal`'s one line).

What a maintainer must not break: the numbers. Scripts test them, and each
tool's documentation publishes its table.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeAlias, TypeVar

from .log import get_logger, redact

__all__ = ["ExitCode", "ExitRule", "Fatal", "exit_code", "extends", "run", "silence_stdout"]

#: A tool's own exit-code enum, as :func:`extends` hands it back.
E = TypeVar("E", bound=type[enum.IntEnum])


class ExitCode(enum.IntEnum):
    """The exit statuses with the same meaning in every tool.

    Small numbers rather than an errno for the tool's own failures, because
    errno values differ between Linux and macOS and collide with 1 and 2
    (``EPERM``, ``ENOENT``).
    """

    #: Everything worked.
    OK = 0
    #: The command ran and something it did failed (some jobs, some files).
    FAILED = 1
    #: The command line is wrong: argparse's own status for a usage error.
    USAGE = 2
    #: A bug: an exception nothing expected (``EX_SOFTWARE``).
    INTERNAL = 70
    #: Ctrl-C: 128 + ``SIGINT``, what the shell reports for a process it killed.
    INTERRUPTED = 130
    #: The reader of standard output went away (``... | head``): 128 + ``SIGPIPE``.
    BROKEN_PIPE = 141


def extends(base: type[enum.IntEnum]) -> Callable[[E], E]:
    """A class decorator: check that a tool's exit-code enum keeps every code of ``base``.

    ``@extends(ExitCode)`` over ``class ExitCode(enum.IntEnum)`` raises
    :class:`TypeError` at import time when the tool's enum lacks one of
    ``base``'s members or gives it another number, so a tool cannot quietly
    drift from the shared table. Restating each member as
    ``OK = xrdcore.cli.ExitCode.OK`` keeps the numbers in one place; the
    member then holds the plain integer, as any ``IntEnum`` member does.
    """

    def check(cls: E) -> E:
        """The decorated enum, unchanged, once it has been found to keep ``base``'s codes."""
        members = cls.__members__
        drifted = [m.name for m in base if m.name not in members or members[m.name] != m]
        if drifted:
            raise TypeError(f"{cls.__name__} does not keep {base.__name__}'s codes: {drifted}")
        return cls

    return check


def silence_stdout() -> None:
    """Point standard output at ``/dev/null`` once the reader of a pipe has gone.

    Otherwise the interpreter's final flush of ``sys.stdout`` fails again at
    exit and prints ``Exception ignored ... BrokenPipeError`` (and exits
    120). This is what the :mod:`signal` module's documentation recommends
    for ``SIGPIPE``. A stream with no file descriptor (a test's capture) is
    left alone.
    """
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):  # io.UnsupportedOperation is a ValueError
        return
    # The descriptor is replaced rather than ``sys.stdout`` reassigned, so the
    # interpreter's own flush at exit - through whatever object it holds -
    # lands somewhere that accepts it.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, fd)
    os.close(devnull)


def run(
    main: Callable[[], int],
    *,
    interrupted: Callable[[], None] | None = None,
    fatal: Callable[[Exception], int] | None = None,
) -> int:
    """Run a command's body and turn the ways it can end into an exit status.

    ``main`` returns the status when it finishes. Ctrl-C is
    :attr:`ExitCode.INTERRUPTED`, after ``interrupted`` (if given) has said
    so; a ``BrokenPipeError`` is :attr:`ExitCode.BROKEN_PIPE`, with nothing
    said - the reader that would have seen it is gone - and stdout silenced
    so the interpreter's exit cannot complain either. Any other exception
    goes to ``fatal``, whose return is the status (the tool reports it and
    maps it to its own table); without ``fatal`` it propagates.

    The broken pipe is caught before ``fatal`` sees anything, although it
    is an ``OSError`` a tool would otherwise call a local fault: ``| head``
    stopping early is how a pipe is used, not a failure.
    """
    try:
        return main()
    except KeyboardInterrupt:
        if interrupted is not None:
            interrupted()
        return int(ExitCode.INTERRUPTED)
    except BrokenPipeError:
        silence_stdout()
        return int(ExitCode.BROKEN_PIPE)
    except Exception as exc:
        if fatal is None:
            raise
        return fatal(exc)


#: One row of a tool's exit-code table: an exception class, and its status -
#: a number, or a function of the exception for a class whose status depends
#: on what it carries (xrdarc's ``ARCError`` is the CE's fault when it names a
#: URL and a local one otherwise).
ExitRule: TypeAlias = tuple[type[BaseException], int | Callable[[Any], int]]


def exit_code(exc: BaseException, table: Iterable[ExitRule]) -> int:
    """The status the first row of ``table`` whose class ``exc`` is an instance of gives.

    Rows are tried in order, so a subclass goes before its base (a usage
    error that is also the tool's general error must be found as the usage
    error). An exception no row names is a bug: :attr:`ExitCode.INTERNAL`.
    """
    for kind, code in table:
        if isinstance(exc, kind):
            return code if isinstance(code, int) else code(exc)
    return ExitCode.INTERNAL


def _str(exc: BaseException) -> str:
    """The exception's own message: :class:`Fatal`'s default ``describe``."""
    return str(exc)


@dataclasses.dataclass(frozen=True)
class Fatal:
    """The handler :func:`run` hands a failure to: say it in one line, return its status.

    ``Fatal("xrdwms", TABLE, logger="xrdwms")`` is passed as ``run``'s
    ``fatal`` (and its :meth:`interrupted` as ``interrupted``). For an
    exception it

    * records it in the active ``--json``/``--xml`` report
      (:func:`xrdcore.report.error`; nothing without one);
    * finds its status in ``codes`` (:func:`exit_code`);
    * says ``prog: message`` on stderr - through the report when there is
      one - where the message is ``describe(exc)`` for a failure the table
      knows and ``internal error: Type: message`` for a bug, whose
      traceback goes to ``logger`` at DEBUG (``-vv``/``-d DEBUG``) instead of
      the terminal. Either way the line is redacted
      (:func:`xrdcore.log.redact`): an error that quotes a URL can quote a
      token with it.

    It can also be called directly, outside :func:`run`, wherever a tool
    reports a failure and carries on (xrdclient's ``fail``). The line is
    ``prog: message`` and not argparse's ``prog: error: message`` because
    that is what each tool has always printed and its tests and users read.
    """

    #: How the line names the tool: ``xrdwms``, or ``xrdarc stat`` for a subcommand.
    prog: str
    #: The tool's exit-code table, most specific class first.
    codes: Sequence[ExitRule]
    #: The message for a failure the table knows; the exception's own text by default.
    describe: Callable[[BaseException], str] = _str
    #: The logger a bug's traceback goes to at DEBUG; the root by default.
    logger: str = ""

    def __call__(self, exc: BaseException) -> int:
        """Report ``exc`` and return its exit status."""
        # Imported here: xrdcore.report imports this module for ExitCode.
        from . import report

        report.error(exc)
        code = exit_code(exc, self.codes)
        if code == ExitCode.INTERNAL:
            get_logger(self.logger).debug("internal error", exc_info=exc)
            self.say(f"internal error: {type(exc).__name__}: {exc}")
        else:
            self.say(self.describe(exc))
        return int(code)

    def interrupted(self) -> None:
        """Say ``prog: interrupted``: what :func:`run` calls on Ctrl-C."""
        self.say("interrupted")

    def say(self, text: str) -> None:
        """One redacted ``prog: text`` line on stderr, or a ``message`` record under a report."""
        from . import report

        report.message(f"{self.prog}: {redact(text)}\n", stderr=True)
