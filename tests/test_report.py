"""The storage-client-report document: JSON and XML, capture, errors, exit codes."""

from __future__ import annotations

import base64
import io
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from xrdcore import report


class Stdout(io.StringIO):
    """A text stdout with a byte buffer, as a real one has."""

    def __init__(self) -> None:
        super().__init__()
        self._bytes = io.BytesIO()

    @property
    def buffer(self) -> io.BytesIO:
        return self._bytes

    def text(self) -> str:
        return self._bytes.getvalue().decode("utf-8") + self.getvalue()


def run(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], body: Callable[[], int], **kw: bool
) -> tuple[int, str]:
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    code = report.run_cli("tool", argv, body, **kw)
    return code, out.text()


@dataclass
class Info:
    name: str
    size: int


class URL(str):
    """Stands in for a client URL type that should print as its string."""


@dataclass(frozen=True)
class DataURL:
    scheme: str
    path: str

    def __str__(self) -> str:
        return f"{self.scheme}://{self.path}"


def test_text_mode_is_untouched(monkeypatch, capsys):
    def body():
        report.record(name="x")
        report.message("hello\n")
        report.message("oops\n", stderr=True)
        return 0

    assert report.run_cli("tool", [], body) == 0
    captured = capsys.readouterr()
    assert captured.out == "hello\n" and captured.err == "oops\n"


def test_json_document_shape(monkeypatch):
    def body():
        report.identify("stat", url="root://h//f")
        report.record(info=Info("f", 3), raw=b"\x00\x01")
        sys.stdout.write("human text\n")
        sys.stdout.buffer.write(b"\xffcontent")
        return 0

    code, text = run(monkeypatch, ["--json"], body)
    document = json.loads(text)
    assert code == 0
    assert document["schema"] == report.SCHEMA and document["version"] == report.VERSION
    assert document["tool"] == document["command"] == "tool"
    kinds = [r["kind"] for r in document["records"]]
    assert kinds == ["result", "message", "content"]
    result = document["records"][0]
    assert result["operation"] == "stat" and result["url"] == "root://h//f"
    assert result["info"] == {"name": "f", "size": 3}
    assert base64.b64decode(result["raw"]["data"]) == b"\x00\x01"
    content = document["records"][2]
    assert base64.b64decode(content["value"]["data"]) == b"\xffcontent"
    assert content["offset"] == 0
    assert document["summary"] == {"exit_code": 0, "ok": True, "error_count": 0, "record_count": 3}


def test_xml_document_round_trips_awkward_strings(monkeypatch):
    def body():
        awkward: dict[str, Any] = {"key\x02": 1.5, "flag": True, "none": None}
        report.record(name="bad\x01name", **awkward)
        return 0

    _, text = run(monkeypatch, ["--output-format", "xml"], body)
    root = ET.fromstring(text)
    assert root.get("schema") == report.SCHEMA
    record = _find(root, "records/record")
    fields = {f.get("name"): f for f in record.findall("field")}
    assert base64.b64decode(fields["name"].text or "").decode() == "bad\x01name"
    encoded_key = next(f for f in record.findall("field") if f.get("name_encoding"))
    assert base64.b64decode(encoded_key.get("name", "")).decode() == "key\x02"
    assert fields["flag"].get("type") == "boolean" and fields["flag"].text == "true"
    assert fields["none"].get("type") == "null"
    assert _find(root, "summary").get("type") == "object"


def test_errors_are_counted_redacted_and_deduplicated(monkeypatch):
    failure = FileNotFoundError(2, "no such file ?authz=secret", "/f")

    def body():
        report.error(failure)
        report.error(failure)
        raise failure

    code, text = run(monkeypatch, ["--json"], body)
    document = json.loads(text)
    errors = [r for r in document["records"] if r["kind"] == "error"]
    assert code == 1 and len(errors) == 1
    assert errors[0]["errno"] == 2 and errors[0]["path"] == "/f"
    assert "secret" not in errors[0]["message"]
    assert document["summary"]["ok"] is False


def test_a_silent_failure_still_gets_a_cause(monkeypatch):
    code, text = run(monkeypatch, ["--json"], lambda: 3)
    document = json.loads(text)
    assert code == 3 and document["summary"]["error_count"] == 1


@pytest.mark.parametrize(("exit_value", "code"), [(None, 0), (4, 4), ("bad usage", 1)])
def test_system_exit(monkeypatch, exit_value, code):
    def body():
        raise SystemExit(exit_value)

    got, text = run(monkeypatch, ["--json"], body)
    document = json.loads(text)
    assert got == code
    if code:
        assert document["summary"]["error_count"] == 1


def test_keyboard_interrupt_is_130(monkeypatch):
    def body():
        raise KeyboardInterrupt

    code, text = run(monkeypatch, ["--json"], body)
    assert code == 130 and json.loads(text)["records"][0]["code"] == 130


def test_parser_errors_become_usage_records(monkeypatch):
    def body():
        parser = report.Parser(prog="tool")
        report.flags(parser)
        parser.add_argument("--n", type=int)
        parser.parse_args(["--json", "--n", "x"])
        return 0

    code, text = run(monkeypatch, ["--json", "--n", "x"], body)
    document = json.loads(text)
    assert code == 2
    assert any(r["kind"] == "error" and r["code"] == 2 for r in document["records"])


def test_legacy_json_prints_the_commands_own_document(monkeypatch):
    def body():
        report.payload({"size": 3, "url": URL("root://h//f")})
        return 0

    _, text = run(monkeypatch, ["--json"], body, legacy_json=True)
    assert json.loads(text) == {"size": 3, "url": "root://h//f"}


def test_payload_without_a_report_prints(capsys):
    report.payload({"a": 1})
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_registered_text_types_print_as_strings(monkeypatch):
    report.register_text_type(DataURL)
    report.register_text_type(DataURL)
    url = DataURL("root", "h//f")

    def body():
        report.record(url=url)
        return 0

    _, text = run(monkeypatch, ["--json"], body)
    assert json.loads(text)["records"][0]["url"] == "root://h//f"
    assert json.loads(report.dumps({"u": url})) == {"u": "root://h//f"}


def test_dumps_refuses_unknown_types():
    with pytest.raises(TypeError, match="object"):
        report.dumps(object())
    assert json.loads(report.dumps({"b": b"\xffx"})) == {"b": "�x"}
    assert json.loads(report.dumps([Info("f", 3)])) == [{"name": "f", "size": 3}]


def test_selection_rules():
    assert report.requested(["--json"])
    assert report.requested(["--output-format=xml"])
    assert not report.requested(["--", "--json"])
    assert not report.requested(["--output-format", "yaml"])
    assert not report.requested(["--output-format=yaml"])
    assert report._selection(["--json", "--xml"]) == ("xml", False)


def test_a_nested_command_writes_into_the_outer_report(monkeypatch):
    def inner():
        report.record(inner=True)
        return 0

    def outer():
        return report.run_cli("inner", ["--json"], inner)

    _, text = run(monkeypatch, ["--json"], outer)
    document = json.loads(text)
    assert [r.get("inner") for r in document["records"]] == [True]


def test_captured_streams_are_not_terminals(monkeypatch):
    def body():
        assert not sys.stdout.isatty()
        with pytest.raises(io.UnsupportedOperation):
            sys.stdout.fileno()
        sys.stdout.flush()
        sys.stdout.buffer.flush()
        return 0

    code, _ = run(monkeypatch, ["--json"], body)
    assert code == 0


def test_a_closed_report_drops_records():
    rep = report.Report("tool", "json")
    rep.finish(0, io.StringIO())
    rep.record("result", x=1)
    rep.error(ValueError("late"))
    assert rep.count == 0 and rep.errors == 0


def test_large_content_is_split_into_bounded_records():
    rep = report.Report("tool", "json")
    out = report.BinaryOutput(rep, "stdout")
    out.write(b"x" * ((1 << 20) + 5))
    rep.closed = True
    offsets = [row["offset"] for row in rep.rows()]
    rep.finish(0, io.StringIO())
    assert offsets == [0, 1 << 20]


def test_broken_pipe_is_exit_1(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Broken())
    assert report.run_cli("tool", ["--json"], lambda: 0) == 1


# ---------------------------------------------------------------------------
# Ported from xrdclient's tests/test_cli_output.py when the module moved here:
# the document through both formats, captured streams, the exception policy
# and the helpers with and without an active report.
# ---------------------------------------------------------------------------


def _find(node: ET.Element, path: str) -> ET.Element:
    """The element at ``path`` below ``node``, which the document must have."""
    found = node.find(path)
    assert found is not None, path
    return found


def test_parse_reads_back_what_finish_wrote_in_either_format():
    """:func:`report.parse` is the format's reader: the XML tree is the JSON one."""
    documents = []
    for fmt in ("json", "xml"):
        rep = report.Report("tool", fmt)
        rep.record("result", **{"key\x02": "bad\x01value", "n": [1, None, True]})
        destination = io.StringIO()
        rep.finish(3, destination)
        documents.append(report.parse(destination.getvalue(), fmt))
    assert documents[0] == documents[1]
    assert documents[1]["records"][0]["key\x02"] == "bad\x01value"
    assert documents[1]["summary"]["exit_code"] == 3


@pytest.mark.parametrize("missing", ["tool", "command", "records", "summary"])
def test_parse_refuses_an_xml_document_that_is_not_a_report(missing):
    parts = {
        "tool": '<tool type="string">t</tool>',
        "command": '<command type="string">c</command>',
        "records": "<records/>",
        "summary": '<summary type="object"/>',
    }
    del parts[missing]
    text = f'<report schema="{report.SCHEMA}" version="1">{"".join(parts.values())}</report>'
    with pytest.raises(ValueError, match=f"no <{missing}>"):
        report.parse(text, "xml")


def test_parse_refuses_a_document_type():
    with pytest.raises(ValueError, match="not allowed"):
        report.parse('<!DOCTYPE report><report schema="s" version="1"/>', "xml")


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_lossless_typed_report(fmt):
    @dataclass
    class Value:
        size: int

    report.register_text_type(DataURL)
    data: dict[str, object] = {"null": None, "bool": True, "false": False, "n": 1.25, "empty": ""}
    data.update({"list": (Value(2), DataURL("root", "h:1094//p")), "blob": bytearray(b"\xff\0")})
    data["\udcff\0"] = "\udcff\0<&>é"
    data["cr"] = "a\rb\r\nc"
    rep = report.Report("test", fmt)
    rep.record("result", value=data)
    destination = io.StringIO()
    rep.finish(0, destination)
    result = report.parse(destination.getvalue(), fmt)
    value = result["records"][0]["value"]
    assert value["list"] == [{"size": 2}, "root://h:1094//p"]
    assert value["blob"] == {"encoding": "base64", "data": "/wA="}
    assert value["\udcff\0"] == "\udcff\0<&>é"
    assert value["null"] is None and value["bool"] is True and value["false"] is False
    assert value["empty"] == "" and value["n"] == 1.25
    assert value["cr"] == "a\rb\r\nc"
    assert result["summary"] == {"exit_code": 0, "ok": True, "error_count": 0, "record_count": 1}
    assert rep.spool.closed


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_binary_chunks_are_bounded_and_lossless(fmt):
    rep = report.Report("cat", fmt)
    stream = report.TextOutput(rep, "stdout")
    rep.identity = {"url": "file:///a"}
    blob = b"\xff\0" * (1 << 20) + b"x"
    assert stream.buffer.write(memoryview(blob)) == len(blob)
    assert stream.buffer.write(b"") == 0
    stream.flush()
    stream.buffer.flush()
    assert not stream.isatty() and stream.encoding == "utf-8"
    with pytest.raises(io.UnsupportedOperation):
        stream.fileno()
    destination = io.StringIO()
    rep.finish(0, destination)
    records = report.parse(destination.getvalue(), fmt)["records"]
    chunks = [base64.b64decode(row["value"]["data"]) for row in records]
    assert b"".join(chunks) == blob
    assert all(len(chunk) <= 1 << 20 for chunk in chunks)
    assert [row["offset"] for row in records] == [0, 1 << 20, 2 << 20]
    assert all(row["url"] == "file:///a" for row in records)


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_streams_messages_errors_and_nested_entrypoints(fmt, capsys, monkeypatch):
    def action():
        report.identify("stat", url="file:///a")
        report.record(value=3)
        report.payload({"a": b"x"})
        report.message("out")
        report.message("err", stderr=True)
        print("print")
        print("stderr", file=sys.stderr)
        active = report.current()
        assert active is not None
        active.command = "stat"
        assert report.run_cli("inner", ["--xml"], lambda: 0) == 0
        exc = OSError(13, "Denied")
        report.error(exc)
        report.error(exc)  # one exception crossing adapters must not be counted twice
        return 0

    monkeypatch.setattr(sys, "argv", ["test", "--output-format", fmt])
    assert report.run_cli("test", None, action) == 0
    captured = capsys.readouterr()
    result = report.parse(captured.out, fmt)
    assert captured.err == "" and report.current() is None
    assert result["command"] == "stat" and not result["summary"]["ok"]
    assert result["summary"]["error_count"] == 1
    error = next(row for row in result["records"] if row["kind"] == "error")
    assert error["code"] == error["errno"] == 13 and error["url"] == "file:///a"
    assert result["records"][0]["value"] == 3


@pytest.mark.parametrize(
    "exc, code",
    [
        (SystemExit(), 0),
        (SystemExit(2), 2),
        (SystemExit("bad"), 1),
        (KeyboardInterrupt(), 130),
        (ValueError("bad"), 1),
    ],
)
def test_reported_exception_policy(exc, code, capsys):
    def action():
        raise exc

    assert report.run_cli("test", ["--xml"], action) == code
    result = report.parse(capsys.readouterr().out, "xml")
    assert result["summary"]["exit_code"] == code
    assert result["summary"]["error_count"] == int(bool(code))


def test_nonzero_return_is_not_silent(capsys):
    assert report.run_cli("test", ["--json"], lambda: 7) == 7
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["code"] == 7


def test_plain_helpers_and_closed_context(capsys):
    report.record(value=object())
    report.identify("plain")
    report.error(ValueError())
    report.payload({"x": b"a"})
    report.message("out")
    report.message("err", stderr=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out.removesuffix("out")) == {"x": "a"}
    assert captured.err == "err"
    assert report.run_cli("test", [], lambda: 9) == 9
    rep = report.Report("test", "json")
    token = report._ACTIVE.set(rep)
    try:
        report.TextOutput(rep, "stdout").write("")
        rep.finish(0, io.StringIO())
        report.record(value="late")
        report.error(ValueError("late"))
        with pytest.raises(SystemExit) as caught:
            report.Parser().error("late")
        assert caught.value.code == 2
        assert rep.count == 0
    finally:
        report._ACTIVE.reset(token)


def test_legacy_payload_compatibility_and_binary_override(capsys):
    def action() -> int:
        report.payload({"v": b"\xff"})
        return 0

    assert report.run_cli("test", ["--json"], action, legacy_json=True) == 0
    assert json.loads(capsys.readouterr().out) == {"v": "�"}

    def binary():
        action()
        sys.stdout.buffer.write(b"\xff")
        return 0

    assert report.run_cli("test", ["--json"], binary, legacy_json=True) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == report.SCHEMA


def test_output_pipe_failure_closes_spool_and_restores_context(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError()

    reports: list[report.Report | None] = []
    monkeypatch.setattr(sys, "stdout", Broken())

    def action() -> int:
        reports.append(report.current())
        return 0

    assert report.run_cli("test", ["--xml"], action) == 1
    (finished,) = reports
    assert finished is not None and finished.spool.closed and report.current() is None


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], ("text", False)),
        (["--", "--json"], ("text", False)),
        (["--output-format"], ("text", False)),
        (["--output-format=json"], ("json", False)),
        (["--xml", "--output-format=bad"], ("xml", False)),
        (["--xml", "--output-format", "bad"], ("xml", False)),
        (["--output-format", "text"], ("text", False)),
        (["--json"], ("json", True)),
    ],
)
def test_format_selection(args, expected):
    assert report._selection(args) == expected
    assert report.requested(args) == (expected[0] != "text")


@pytest.mark.parametrize("legacy", [False, True])
def test_conflicting_flags_are_structured_usage_errors(legacy, capsys):
    parser = report.Parser()
    report.flags(parser, legacy_json=legacy)

    def conflicting() -> int:
        parser.parse_args(["--json", "--xml"])  # exits with the usage error
        return 0

    assert report.run_cli("test", ["--json", "--xml"], conflicting) == 2
    result = report.parse(capsys.readouterr().out, "xml")
    assert result["records"][0]["error_type"] == "ValueError"


def test_non_finite_and_unknown_values_are_rejected():
    rep = report.Report("test", "json")
    with pytest.raises(ValueError):
        rep.record("result", value=float("nan"))
    with pytest.raises(TypeError):
        rep.record("result", value=object())
    rep.finish(1, io.StringIO())
