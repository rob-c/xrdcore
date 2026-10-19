"""The shared exit statuses, the ``extends`` check, how :func:`xrdcore.cli.run` ends a command,
and the :class:`~xrdcore.cli.Fatal` handler.

xrdarc's and xrdwms's command-line suites test their own tables and
messages; this file pins the numbers every tool shares, the three ways
out of :func:`~xrdcore.cli.run` (Ctrl-C, a closed pipe, any other failure)
and what the shared handler says, records and returns.
"""

from __future__ import annotations

import enum
import io
import json
import logging
import os
import sys

import pytest

from xrdcore import cli, report


def test_the_shared_numbers_are_the_documented_ones():
    assert {code.name: int(code) for code in cli.ExitCode} == {
        "OK": 0,
        "FAILED": 1,
        "USAGE": 2,
        "INTERNAL": 70,
        "INTERRUPTED": 130,
        "BROKEN_PIPE": 141,
    }


def test_a_tool_enum_that_keeps_every_shared_code_passes_unchanged():
    @cli.extends(cli.ExitCode)
    class Tool(enum.IntEnum):
        OK = cli.ExitCode.OK
        FAILED = cli.ExitCode.FAILED
        USAGE = cli.ExitCode.USAGE
        OWN = 3
        INTERNAL = cli.ExitCode.INTERNAL
        INTERRUPTED = cli.ExitCode.INTERRUPTED
        BROKEN_PIPE = cli.ExitCode.BROKEN_PIPE

    assert (int(Tool.OWN), int(Tool.INTERRUPTED)) == (3, 130)
    assert type(Tool.OK.value) is int


def test_a_tool_enum_missing_or_renumbering_a_shared_code_is_refused():
    with pytest.raises(TypeError, match=r"\['INTERNAL', 'INTERRUPTED', 'BROKEN_PIPE'\]"):

        @cli.extends(cli.ExitCode)
        class Tool(enum.IntEnum):
            OK = 0
            FAILED = 1
            USAGE = 2
            INTERNAL = 71  # renumbered; the last two are missing


def test_run_returns_what_the_command_returns():
    assert cli.run(lambda: 6) == 6


def test_ctrl_c_is_130_after_the_tool_has_said_so():
    said = []

    def interrupted() -> int:
        raise KeyboardInterrupt

    assert cli.run(interrupted, interrupted=lambda: said.append("interrupted")) == 130
    assert said == ["interrupted"]
    assert cli.run(interrupted) == 130


def test_a_closed_pipe_is_a_quiet_141_never_handed_to_fatal(monkeypatch):
    silenced = []
    monkeypatch.setattr(cli, "silence_stdout", lambda: silenced.append(1))

    def broken() -> int:
        raise BrokenPipeError

    def fatal(exc: Exception) -> int:
        raise AssertionError("a broken pipe is not a failure")

    assert cli.run(broken, fatal=fatal) == 141
    assert silenced == [1]


def test_any_other_failure_goes_to_fatal_or_propagates():
    def failing() -> int:
        raise ValueError("bad")

    seen: list[Exception] = []

    def fatal(exc: Exception) -> int:
        seen.append(exc)
        return 5

    assert cli.run(failing, fatal=fatal) == 5
    assert isinstance(seen[0], ValueError)
    with pytest.raises(ValueError, match="bad"):
        cli.run(failing)


def test_silence_stdout_points_the_descriptor_at_devnull(tmp_path, monkeypatch):
    target = tmp_path / "out"
    with target.open("w") as stream:
        monkeypatch.setattr(sys, "stdout", stream)
        cli.silence_stdout()
        os.write(stream.fileno(), b"gone")
    assert target.read_bytes() == b""


@pytest.mark.parametrize("stream", [io.StringIO(), object()])
def test_silence_stdout_leaves_a_stream_without_a_descriptor_alone(monkeypatch, stream):
    monkeypatch.setattr(sys, "stdout", stream)
    cli.silence_stdout()


class _Usage(ValueError):
    """A tool's usage error: a ``ValueError`` that must be found before ``ValueError``."""


class _Remote(OSError):
    """A tool error whose status depends on what it carries."""

    def __init__(self, url: str | None) -> None:
        super().__init__("the CE failed")
        self.url = url


def _remote_code(exc: _Remote) -> int:
    return 5 if exc.url else 6


_TABLE: tuple[cli.ExitRule, ...] = (
    (_Usage, cli.ExitCode.USAGE),
    (_Remote, _remote_code),
    (ValueError, 6),
)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (_Usage("x"), 2),
        (ValueError("x"), 6),
        (_Remote("https://ce"), 5),
        (_Remote(None), 6),
        (RuntimeError("x"), 70),
    ],
)
def test_exit_code_takes_the_first_matching_row_else_internal(exc, code):
    assert cli.exit_code(exc, _TABLE) == code


def test_exit_code_keeps_the_tools_own_member():
    class Tool(enum.IntEnum):
        LOCAL = 6

    assert cli.exit_code(OSError(), [(OSError, Tool.LOCAL)]) is Tool.LOCAL


def test_fatal_says_a_known_failure_in_one_redacted_line(capsys):
    fatal = cli.Fatal("tool sub", _TABLE)
    assert fatal(ValueError("bad https://h/f?authz=s3cr3t")) == 6
    assert capsys.readouterr().err == "tool sub: bad https://h/f?authz=<redacted>\n"


def test_fatal_uses_the_tools_describe(capsys):
    fatal = cli.Fatal("tool", _TABLE, describe=lambda exc: f"<{exc}>")
    assert fatal(_Usage("no jobs")) == 2
    assert capsys.readouterr().err == "tool: <no jobs>\n"


def test_a_bug_is_one_line_with_its_traceback_logged_at_debug(capsys, caplog):
    fatal = cli.Fatal("tool", _TABLE, logger="xrdcore.tests.fatal")
    with caplog.at_level(logging.DEBUG, logger="xrdcore.tests.fatal"):
        assert fatal(KeyError("k")) == 70
    assert capsys.readouterr().err == "tool: internal error: KeyError: 'k'\n"
    (record,) = caplog.records
    assert record.name == "xrdcore.tests.fatal" and record.levelno == logging.DEBUG
    assert record.exc_info is not None and isinstance(record.exc_info[1], KeyError)


def test_fatal_says_interrupted_for_run(capsys):
    fatal = cli.Fatal("tool", _TABLE)

    def stopped() -> int:
        raise KeyboardInterrupt

    def failing() -> int:
        raise _Usage("wrong")

    assert cli.run(stopped, interrupted=fatal.interrupted, fatal=fatal) == 130
    assert cli.run(failing, interrupted=fatal.interrupted, fatal=fatal) == 2
    assert capsys.readouterr().err == "tool: interrupted\ntool: wrong\n"


def test_under_a_report_fatal_records_the_error_and_one_message(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    fatal = cli.Fatal("tool", _TABLE)

    def failing() -> int:
        raise _Remote("https://ce")

    assert report.run_cli("tool", ["--json"], lambda: cli.run(failing, fatal=fatal)) == 5
    records = json.loads(out.getvalue())["records"]
    assert [r["kind"] for r in records] == ["error", "message"]
    assert (records[1]["stream"], records[1]["text"]) == ("stderr", "tool: the CE failed\n")
