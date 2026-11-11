"""``did you mean`` for a mis-typed command line: what is offered, and what is not.

The parser below is the one every tool on the stack builds, so these cases are
the behaviour of ``xrd-cp``, ``xrd-fs``, ``xrd-proxy``, the ``gfal-*``
commands, ``xrdarc`` and ``xrdwms``. Each of those drives its own entry point
in its own repository; this is the shared machinery they inherit.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from xrdcore import report, suggest


def refused(capsys: pytest.CaptureFixture[str], argv: list[str], *, commands: bool = False) -> str:
    """The stderr the chain's parser prints for ``argv``, which it must refuse."""
    parser = report.Parser(prog="tool")
    parser.add_argument("--long", action="store_true")
    parser.add_argument("file", nargs="?")
    if commands:
        sub = parser.add_subparsers(dest="command", required=True, parser_class=report.Parser)
        init = sub.add_parser("init", allow_abbrev=False)
        init.add_argument("-valid", metavar="HH:MM")
        init.add_argument("--profile", choices=("rfc3820", "arc", "xrootd"))
        sub.add_parser("info", allow_abbrev=False)
    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


def test_a_mis_typed_option_names_the_right_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert "did you mean '--long'?" in refused(capsys, ["--longg"])


def test_a_mis_typed_option_after_a_subcommand_is_still_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``argparse`` reports it at the top level, so the subcommands' options are consulted."""
    assert "did you mean '-valid'?" in refused(capsys, ["init", "-vaild", "1:00"], commands=True)


def test_a_mis_typed_option_with_a_value_is_named_by_its_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "did you mean '--long'?" in refused(capsys, ["--longg=1"])


def test_a_mis_typed_subcommand_names_the_right_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "did you mean 'init'?" in refused(capsys, ["inti"], commands=True)


def test_a_value_outside_a_choice_set_names_the_right_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "did you mean 'arc'?" in refused(capsys, ["init", "--profile", "arcx"], commands=True)


def test_nothing_is_offered_when_nothing_is_close(capsys: pytest.CaptureFixture[str]) -> None:
    assert "did you mean" not in refused(capsys, ["--zzzz"])
    assert "did you mean" not in refused(capsys, ["zzz"], commands=True)


def test_a_short_form_of_a_longer_name_is_offered(capsys: pytest.CaptureFixture[str]) -> None:
    """``submitt`` for ``sub``: too many edits away, but the command is spelled shortly."""
    parser = report.Parser(prog="tool")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sub")
    sub.add_parser("stats")
    message = "argument command: invalid choice: 'submitt' (choose from 'sub', 'stats')"
    assert suggest.suggestion(parser, message) == "; did you mean 'sub'?"
    # An abbreviation the other way round: ``--lon`` for ``--long``.
    flags = report.Parser(prog="tool")
    flags.add_argument("--long", action="store_true")
    assert suggest.suggestion(flags, "unrecognized arguments: --lon") == "; did you mean '--long'?"


def test_a_name_too_short_to_guess_from_is_left_alone() -> None:
    """``-k`` is one edit from ``-l`` and from every other one-letter option."""
    parser = report.Parser(prog="tool")
    parser.add_argument("-l", action="store_true")
    assert suggest.suggestion(parser, "unrecognized arguments: -k") == ""


def test_a_longer_wrong_name_does_not_win_over_a_short_form() -> None:
    """A real spelling fix scores ahead of a shared prefix."""
    parser = report.Parser(prog="tool")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--start", action="store_true")
    assert suggest.suggestion(parser, "unrecognized arguments: --sattus") == (
        "; did you mean '--status'?"
    )


def test_a_stray_positional_is_not_guessed_at(capsys: pytest.CaptureFixture[str]) -> None:
    """There is no list a bare word came from, so nothing is offered for one."""
    assert "did you mean" not in refused(capsys, ["one", "two"])


def test_an_error_about_something_else_is_left_alone() -> None:
    parser = report.Parser(prog="tool")
    parser.add_argument("--long", action="store_true")
    assert suggest.suggestion(parser, "the following arguments are required: file") == ""
    assert suggest.suggestion(parser, "unrecognized arguments: plain-word") == ""


def test_a_parser_registered_twice_is_walked_once() -> None:
    """The option walk visits each parser once, even one reachable under two names."""
    top = report.Parser(prog="tool")
    commands = top.add_subparsers(dest="command")
    first = commands.add_parser("first")
    first.add_argument("--only", action="store_true")
    commands._name_parser_map["second"] = first  # the same parser, a second name
    assert suggest.suggestion(top, "unrecognized arguments: --onyl") == "; did you mean '--only'?"


def test_the_suggestion_reaches_the_json_report(capsys: pytest.CaptureFixture[str]) -> None:
    """The one line on stderr and the ``error`` record a script reads say the same thing."""

    def body() -> int:
        parser = report.Parser(prog="tool")
        report.flags(parser)
        parser.add_argument("--long", action="store_true")
        parser.parse_args(["--json", "--longg"])
        return 0

    code = report.run_cli("tool", ["--json", "--longg"], body)
    document: dict[str, Any] = json.loads(capsys.readouterr().out)
    [error] = [record for record in document["records"] if record["kind"] == "error"]
    assert code == 2
    assert "did you mean '--long'?" in error["message"]


def test_a_plain_argparse_parser_keeps_its_own_wording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the chain's parser answers: one implementation, in one place."""
    parser = argparse.ArgumentParser(prog="plain")
    parser.add_argument("--long", action="store_true")
    with pytest.raises(SystemExit):
        parser.parse_args(["--longg"])
    assert "did you mean" not in capsys.readouterr().err
