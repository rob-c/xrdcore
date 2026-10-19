"""Versioned CLI reports shared by every storage tool, without service dependencies.

Records are spooled, not accumulated in memory. Binary streams become bounded
base64 records. Human output remains a supplemental message, never the source
of a result's fields or error code.

xrdclient's ``xrd-cp``/``xrd-fs``, xgfalclient's ``gfal-*`` commands and
``xrdarc`` all write this document, so its format is shared API: a script
written against one tool's ``--json`` reads the others'. The format is::

    {"schema": "storage-client-report", "version": 1, "tool": ..., "command": ...,
     "records": [{"sequence": 1, "kind": "result" | "data" | "content" |
                  "message" | "error" | ..., ...}, ...],
     "summary": {"exit_code": N, "ok": bool, "error_count": N, "record_count": N}}

with an XML rendering of the same tree for ``--xml``; :func:`parse` reads
either back as that tree. Bump :data:`VERSION`
when a change would make an existing consumer misread the document: a field
removed, renamed or retyped, a meaning changed. New record kinds and new
fields are *not* a bump - consumers are told to ignore what they do not
know - and neither is a new tool name.

Writing a tool on it: build the parser as :class:`Parser`, add :func:`flags`,
call :func:`identify` once the command is known, then :func:`record` /
:func:`error` / :func:`message` as it runs, and hand the whole thing to
:func:`run_cli` - which owns the exit code and the document. Nothing here
needs a report to be active: with ``--output-format text`` the same calls
print, or do nothing, so a command has one code path.

How it works: :func:`run_cli` wraps a command, swaps ``sys.stdout`` and
``sys.stderr`` for :class:`TextOutput` objects so that anything the command
prints becomes a ``message`` record, and installs a :class:`Report` in a
context variable. Commands call the module-level helpers (:func:`record`,
:func:`error`, :func:`payload`, :func:`message`) unconditionally; with no
report active they print as a text tool would, or do nothing.

A tool's own value types reach a record as whatever :func:`_normal` makes of
them: dataclasses become objects, bytes become base64. A type that reads
better as its string - a URL class that happens to be a dataclass - is
registered once with :func:`register_text_type`.

``tests/test_import_cost.py`` holds this module to the cheap-import rule:
it must never import a client, TLS or crypto stack, because it runs before
``--help`` has been answered.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import dataclasses
import io
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, NoReturn, TextIO, cast
from xml.etree import ElementTree as ET

from .cli import ExitCode
from .log import redact
from .safexml import fromstring

__all__ = [
    "SCHEMA",
    "VERSION",
    "BinaryOutput",
    "Parser",
    "Report",
    "TextOutput",
    "current",
    "dumps",
    "error",
    "flags",
    "identify",
    "message",
    "parse",
    "payload",
    "record",
    "register_text_type",
    "requested",
    "run_cli",
]

#: The document's ``schema`` field: names the format, not the tool.
SCHEMA = "storage-client-report"
#: The format's version; consumers key on it before reading ``records``.
VERSION = 1
# A context variable rather than a module global so that a test (or an
# embedding program) running one command inside another's report - or two in
# different contexts - never writes into the wrong document.
_ACTIVE: contextvars.ContextVar[Report | None] = contextvars.ContextVar("cli_report", default=None)
# Characters XML 1.0 cannot carry in text or attribute values: C0 controls
# other than tab and LF, lone surrogates (which ``os.fsdecode`` produces for
# undecodable filename bytes), and the two noncharacters. XML parsers
# normalise literal CR characters, so encode those as well.
_INVALID_XML = re.compile("[\x00-\x08\x0b-\x1f\ud800-\udfff\ufffe\uffff]")


#: Types written as ``str(value)`` rather than taken apart: see
#: :func:`register_text_type`. A tuple, replaced and never mutated, so a
#: reader on another thread sees either the old set or the new one.
_TEXT_TYPES: tuple[type, ...] = ()


def register_text_type(cls: type) -> None:
    """Write instances of ``cls`` into reports (and :func:`dumps`) as ``str(value)``.

    For a value type whose string *is* its meaning - a URL - but which would
    otherwise be taken apart field by field because it is a dataclass.
    Registering a type twice is harmless.
    """
    global _TEXT_TYPES
    if cls not in _TEXT_TYPES:
        _TEXT_TYPES = (*_TEXT_TYPES, cls)


def _plain(obj: Any) -> Any:
    """:func:`json.dumps`' ``default``: a tool's own types, without teaching them JSON."""
    if _TEXT_TYPES and isinstance(obj, _TEXT_TYPES):
        return str(obj)  # before the dataclass branch: a URL is one, and reads badly as one
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def dumps(value: object) -> str:
    """The legacy ``--json`` rendering: stable key order, two-space indent.

    This is the single document a 0.3-era ``--json`` printed, kept so the
    scripts that parse it keep working (see :func:`flags`). Bytes are decoded
    leniently here - that format never carried file content - where a report
    record carries them as base64.
    """
    return json.dumps(value, default=_plain, indent=2, sort_keys=False)


def current() -> Report | None:
    """The report the running command writes into, or ``None`` for text output."""
    return _ACTIVE.get()


class Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors become ``error`` records.

    Plain argparse prints usage to stderr and exits ``2``; under ``--json`` that
    stderr text is captured as a ``message`` record, but a consumer needs a
    numeric error too, or the document's ``ok: false`` would have no cause.
    """

    def error(self, message: str) -> NoReturn:
        """Record the usage error with code ``2``, then let argparse exit."""
        report = current()
        if report is not None and report.closed:
            # The document has already been written: adding to it now would
            # be lost, and argparse's own usage text would land on the real
            # stdout after the closing brace.
            raise SystemExit(2)
        error(ValueError(message), code=2)
        super().error(message)


def flags(parser: argparse.ArgumentParser, *, legacy_json: bool = False) -> None:
    """Add ``--json``, ``--xml`` and ``--output-format`` as one exclusive group.

    ``legacy_json`` keeps the 0.3 meaning of ``--json`` for ``xrd-cp`` and
    ``xrd-fs``: a store-true flag the command reads itself to print its own
    single JSON document, which :meth:`Report.finish` still emits when that
    is all the command produced. ``--output-format json`` always means the
    versioned report. The format itself is chosen from ``argv`` by
    :func:`_selection` before parsing, so apart from the legacy ``args.json``
    that commands test, these arguments exist for ``--help`` and validation.
    """
    group = parser.add_mutually_exclusive_group()
    if legacy_json:
        group.add_argument("--json", action="store_true", help="legacy JSON result format")
    else:
        group.add_argument(
            "--json",
            dest="output_format",
            action="store_const",
            const="json",
            help="versioned JSON report",
        )
    group.add_argument(
        "--xml",
        dest="output_format",
        action="store_const",
        const="xml",
        help="versioned XML report",
    )
    group.add_argument(
        "--output-format",
        choices=("text", "json", "xml"),
        help="output format; JSON/XML include results, messages and numeric errors",
    )


def _selection(argv: Sequence[str]) -> tuple[str, bool]:
    """The output format ``argv`` asks for, and whether it came from ``--json``.

    This scans the raw arguments rather than waiting for argparse, because
    the report has to exist *before* parsing: a mistyped flag must already be
    reported as a structured ``error`` record. The last format flag wins, as
    argparse's own ``store`` actions do, and ``--`` ends the scan so a file
    named ``--json`` is an operand. An ``--output-format`` value outside the
    known three is left to argparse to reject.
    """
    selected, legacy = "text", False
    for index, value in enumerate(argv):
        if value == "--":
            break
        if value in ("--json", "--xml"):
            selected, legacy = value[2:], value == "--json"
        elif value.startswith("--output-format="):
            candidate = value.partition("=")[2]
            if candidate in ("text", "json", "xml"):
                selected, legacy = candidate, False
        elif (
            value == "--output-format"
            and index + 1 < len(argv)
            and argv[index + 1] in ("text", "json", "xml")
        ):
            selected, legacy = argv[index + 1], False
    return selected, legacy


def requested(argv: Sequence[str]) -> bool:
    """Whether ``argv`` asks for a machine-readable document (JSON or XML)."""
    return _selection(argv)[0] in ("json", "xml")


def _normal(value: Any) -> Any:
    """``value`` reduced to JSON types: dicts, lists, strings, numbers, ``None``.

    Bytes become ``{"encoding": "base64", "data": ...}`` rather than a decoded
    string, because file content and checksums are not text and a lossy
    decode would make the record useless for reassembly.
    """
    if _TEXT_TYPES and isinstance(value, _TEXT_TYPES):
        # Before the dataclass branch: a URL is one, and reads badly as one.
        return str(value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normal(
            {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
        )
    elif isinstance(value, dict):
        return {str(key): _normal(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    elif isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _xml(parent: ET.Element, tag: str, value: Any, **attrs: str) -> None:
    """Append ``value`` (already :func:`_normal`) to ``parent`` as typed XML.

    The mapping is JSON's, made explicit: every element has a ``type`` of
    ``object``, ``array``, ``string``, ``number``, ``boolean`` or ``null``,
    objects hold ``<field name=...>`` children and arrays ``<item>`` children,
    so the XML and JSON documents round-trip to the same tree.
    """
    node = ET.SubElement(parent, tag, attrs)
    if isinstance(value, dict):
        node.set("type", "object")
        for key, item in value.items():
            attrs = {"name": key}
            # A key can be a filename, so it can hold what XML cannot.
            if _INVALID_XML.search(key):
                attrs = {"name": _encoded(key), "name_encoding": "utf8-surrogatepass-base64"}
            _xml(node, "field", item, **attrs)
    elif isinstance(value, list):
        node.set("type", "array")
        for item in value:
            _xml(node, "item", item)
    else:
        _scalar(node, value)


def _scalar(node: ET.Element, value: Any) -> None:
    """Set ``node``'s type and text for one JSON scalar.

    Strings XML cannot hold are base64-encoded with an ``encoding`` attribute
    naming the transform, rather than replaced, so a filename with a control
    character or undecodable byte in it survives the trip exactly. Numbers go
    through :func:`json.dumps` so XML and JSON spell them identically, and NaN
    or infinity is an error in both.
    """
    if value is None:
        node.set("type", "null")
    elif isinstance(value, str):
        node.set("type", "string")
        if _INVALID_XML.search(value):
            node.set("encoding", "utf8-surrogatepass-base64")
            value = _encoded(value)
        node.text = value
    else:
        node.set("type", "boolean" if isinstance(value, bool) else "number")
        node.text = json.dumps(value, allow_nan=False)


def _encoded(value: str) -> str:
    # ``surrogatepass`` keeps the lone surrogates ``os.fsdecode`` uses for
    # undecodable bytes, so the decoder can recover the original name.
    return base64.b64encode(value.encode("utf-8", "surrogatepass")).decode("ascii")


def _decoded(value: str) -> str:
    """The string :func:`_encoded` made ``value`` from, lone surrogates and all."""
    return base64.b64decode(value).decode("utf-8", "surrogatepass")


def parse(text: str, output_format: str) -> dict[str, Any]:
    """A finished document as the JSON tree it is, whichever format it was written in.

    ``output_format`` is what the :class:`Report` was made with: ``json``,
    or anything else for XML, as :meth:`Report.finish` decides. The XML
    rendering comes back as the same dicts, lists and scalars the JSON one
    does - encoded strings and keys decoded - so a consumer (or a test)
    reads both with one code path. The XML is parsed with
    :func:`xrdcore.safexml.fromstring`, so a document type is refused.
    Raises :class:`ValueError` for a document that is not a report.
    """
    if output_format == "json":
        document: dict[str, Any] = json.loads(text)
        return document
    root = fromstring(text)
    return {
        "schema": root.attrib["schema"],
        "version": int(root.attrib["version"]),
        "tool": _value(_child(root, "tool")),
        "command": _value(_child(root, "command")),
        "records": [_value(node) for node in _child(root, "records")],
        "summary": _value(_child(root, "summary")),
    }


def _child(root: ET.Element, tag: str) -> ET.Element:
    """``root``'s ``tag`` element, which every report has; :class:`ValueError` if missing."""
    found = root.find(tag)
    if found is None:
        raise ValueError(f"not a {SCHEMA} document: no <{tag}>")
    return found


def _value(node: ET.Element) -> Any:
    """The JSON value one typed element carries: the inverse of :func:`_xml`."""
    kind = node.attrib["type"]
    if kind == "object":
        return {_name(field): _value(field) for field in node}
    if kind == "array":
        return [_value(item) for item in node]
    if kind == "null":
        return None
    text = node.text or ""
    if kind == "string":
        return _decoded(text) if "encoding" in node.attrib else text
    return json.loads(text)


def _name(field: ET.Element) -> str:
    """An object field's key, decoded if it had to be encoded."""
    name = field.attrib["name"]
    return _decoded(name) if "name_encoding" in field.attrib else name


def _spool() -> tempfile.SpooledTemporaryFile[str]:
    """A text spool for a :class:`Report`'s records, owned and closed by its :meth:`~Report.finish`.

    In memory up to 1 MiB, a real temporary file after that.
    """
    return tempfile.SpooledTemporaryFile(
        max_size=1 << 20, mode="w+", encoding="utf-8", newline="\n"
    )


class Report:
    """One invocation's structured document, built as the command runs.

    Records are written to a spool as JSON lines the moment they happen and
    only assembled into the final document by :meth:`finish`, so ``xrd-fs cat``
    of a multi-gigabyte file under ``--json`` costs a temporary file, not that
    much memory. ``identity`` is the context the command has declared
    (operation, URL) and is merged into every record it writes.

    Lifetime: created and finished by :func:`run_cli`; once :attr:`closed`,
    further records are dropped silently rather than raising, because a
    worker thread or ``atexit`` hook can still be talking after the document
    is out.
    """

    def __init__(self, tool: str, output_format: str, *, legacy: bool = False) -> None:
        """An empty report for ``tool`` in ``output_format`` (``json`` or ``xml``)."""
        self.tool, self.output_format, self.legacy = tool, output_format, legacy
        self.command = tool
        self.identity: dict[str, Any] = {}
        self.count = self.errors = 0
        self.closed = False
        # The legacy ``--json`` document, if the command produced one with
        # :func:`payload`; emitted instead of the report when it is all there is.
        self.payload: Any = None
        self.has_payload = False
        self.has_content = False
        # The last error recorded and the identity it was recorded under, so
        # that the same failure reported twice - once by the command and once
        # by the wrapper that caught it - counts once.
        self.last_error: BaseException | None = None
        self.last_error_identity: dict[str, Any] = {}
        # Re-entrant: :meth:`error` calls :meth:`record` with the lock held.
        # A lock at all because a command may record from several threads
        # (the gfal tools' progress thread, transfer callbacks).
        self.lock = threading.RLock()
        self.spool = _spool()

    def record(self, kind: str, **fields: Any) -> None:
        """Append one record of ``kind``; numbering is by arrival, from 1."""
        with self.lock:
            if self.closed:
                return
            value = {"sequence": self.count + 1, "kind": kind, **fields}
            # ensure_ascii so a spooled line can never hold a raw newline or a
            # surrogate the UTF-8 spool cannot encode: one record, one line.
            self.spool.write(json.dumps(_normal(value), ensure_ascii=True, allow_nan=False) + "\n")
            self.count += 1
            self.has_content |= kind == "content"

    def error(self, exc: BaseException, **fields: Any) -> None:
        """Record ``exc`` as an ``error`` record and count it against ``ok``.

        ``code`` is the exception's own ``code`` (an XRootD ``kXR_*`` status,
        a gfal2 error code) or its ``errno``; ``fields``
        override anything derived. The message is redacted, as a log line is.
        """
        with self.lock:
            identity = {**self.identity, **fields}
            if self.closed or (self.last_error is exc and self.last_error_identity == identity):
                return
            self.last_error = exc
            self.last_error_identity = identity
            self.errors += 1
            self.record(
                "error",
                **{
                    **self.identity,
                    "code": getattr(exc, "code", getattr(exc, "errno", None)),
                    "errno": getattr(exc, "errno", None),
                    "path": getattr(exc, "path", getattr(exc, "filename", None)),
                    "error_type": type(exc).__name__,
                    # Redacted as a log line is: a message can quote a URL with a token in it.
                    "message": redact(getattr(exc, "user_message", str(exc))),
                    **fields,
                },
            )

    def rows(self) -> Iterator[dict[str, Any]]:
        """The spooled records in order, decoded; only valid once closed."""
        self.spool.seek(0)
        for line in self.spool:
            yield json.loads(line)

    def finish(self, code: int, stream: TextIO) -> None:
        """Close the report and write the whole document to ``stream``.

        The spool is released whether or not writing succeeds; a broken pipe
        propagates to :func:`run_cli`, which turns it into exit code ``1``.
        """
        with self.lock:
            self.closed = True
        summary = {
            "exit_code": code,
            "ok": code == 0 and self.errors == 0,
            "error_count": self.errors,
            "record_count": self.count,
        }
        try:
            # 0.3 ``--json`` printed the command's own document and nothing
            # else; scripts parse that, so it stays the output whenever the
            # command produced one. File content cannot be expressed in it,
            # so a ``cat`` falls through to the versioned report.
            if self.legacy and self.has_payload and not self.has_content:
                stream.write(dumps(self.payload) + "\n")
            elif self.output_format == "json":
                self._json(stream, summary)
            else:
                self._xml(stream, summary)
            stream.flush()
        finally:
            self.spool.close()

    def _json(self, stream: TextIO, summary: dict[str, Any]) -> None:
        """Stream the JSON document: header, records one at a time, summary."""
        header = {"schema": SCHEMA, "version": VERSION, "tool": self.tool, "command": self.command}
        # Drop the header's closing brace and splice the records array in by
        # hand: building one dict of every record would undo the spooling.
        stream.write(json.dumps(header)[:-1] + ', "records": [')
        for index, row in enumerate(self.rows()):
            stream.write((", " if index else "") + json.dumps(row))
        stream.write('], "summary": ' + json.dumps(summary) + "}\n")

    def _xml(self, stream: TextIO, summary: dict[str, Any]) -> None:
        """Stream the XML document, one serialised ``<record>`` at a time."""
        header = ET.Element("report", schema=SCHEMA, version=str(VERSION))
        _xml(header, "tool", self.tool)
        _xml(header, "command", self.command)
        stream.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        # [:-9] strips the closing "</report>" so the records can be streamed
        # inside it; the footer below writes it back.
        stream.write(ET.tostring(header, encoding="unicode")[:-9] + "<records>")
        for row in self.rows():
            wrapper = ET.Element("records")
            _xml(wrapper, "record", row)
            stream.write(ET.tostring(wrapper[0], encoding="unicode"))
        footer = ET.Element("report")
        _xml(footer, "summary", summary)
        stream.write("</records>" + ET.tostring(footer[0], encoding="unicode") + "</report>\n")


class BinaryOutput:
    """The ``.buffer`` of a captured stream: bytes become ``content`` records.

    Each record carries at most 1 MiB, base64-encoded, and the byte ``offset``
    it starts at, so a consumer reassembles the stream by concatenating the
    decoded values in sequence order and can check for gaps.
    """

    def __init__(self, report: Report, stream: str) -> None:
        """Capture into ``report`` under the stream name ``stdout``/``stderr``."""
        self.report, self.stream = report, stream
        self.offset = 0

    def write(self, data: bytes | bytearray | memoryview) -> int:
        """Record ``data`` in bounded slices; always accepts all of it."""
        for start in range(0, len(data), 1 << 20):
            chunk = memoryview(data)[start : start + (1 << 20)]
            self.report.record(
                "content",
                stream=self.stream,
                offset=self.offset,
                **self.report.identity,
                value=chunk,
            )
            self.offset += len(chunk)
        return len(data)

    def flush(self) -> None:
        """Nothing to do: every write is already a record."""


class TextOutput:
    """What ``sys.stdout``/``sys.stderr`` are while a report is active.

    Text becomes ``message`` records tagged with the stream it was meant for,
    and ``.buffer`` (where a ``cat`` writes file content) becomes ``content``
    records. It says it is not a terminal so
    progress bars and credential prompts stay quiet, and it has no file
    descriptor, so anything that would write past Python to fd 1 fails loudly
    instead of corrupting the document.
    """

    encoding = "utf-8"

    def __init__(self, report: Report, stream: str) -> None:
        """Capture into ``report`` under the stream name ``stdout``/``stderr``."""
        self.report, self.stream = report, stream
        self.buffer = BinaryOutput(report, stream)

    def write(self, text: str) -> int:
        """Record ``text`` as one message; empty writes leave no record."""
        if text:
            self.report.record("message", stream=self.stream, text=text)
        return len(text)

    def flush(self) -> None:
        """Nothing to do: every write is already a record."""

    def isatty(self) -> bool:
        """Never a terminal: nobody is reading this stream as it happens."""
        return False

    def fileno(self) -> int:
        """Refuse: there is no descriptor behind a structured report."""
        raise io.UnsupportedOperation("a structured report is not a raw file descriptor")


class _Destination:
    """Machine documents use UTF-8 even when stdout has a legacy locale encoding.

    A batch node with ``LANG=C`` gives Python an ASCII stdout; writing the
    document's bytes to the underlying buffer sidesteps that. The records
    themselves are ASCII already (see :meth:`Report.record`), but tool and
    command names and the summary pass through here too.
    """

    def __init__(self, stream: TextIO) -> None:
        """Write to ``stream``'s byte buffer when it has one, else to it."""
        self.stream = stream
        self.buffer = getattr(stream, "buffer", None)

    def write(self, text: str) -> None:
        """Write ``text`` UTF-8 encoded."""
        if self.buffer is None:
            self.stream.write(text)
        else:
            self.buffer.write(text.encode("utf-8"))

    def flush(self) -> None:
        """Flush the text stream; its buffer is flushed through it."""
        self.stream.flush()


def record(kind: str = "result", **fields: Any) -> None:
    """Add a record to the active report, tagged with its identity; else nothing.

    Commands call this alongside their human output, never instead of it, so
    the text and the structured result cannot drift apart.
    """
    report = current()
    if report is not None:
        report.record(kind, **{**report.identity, **fields})


def identify(operation: str, **fields: Any) -> None:
    """Declare what the command is doing; later records carry these fields.

    Replaces, rather than merges, the identity: each operation in a
    multi-operation command starts from a clean slate.
    """
    report = current()
    if report is not None:
        report.identity = {"operation": operation, **fields}


def error(exc: BaseException, **fields: Any) -> None:
    """Record a failure in the active report; the caller still prints it."""
    report = current()
    if report is not None:
        report.error(exc, **fields)


def payload(value: Any, *, where: TextIO | None = None) -> None:
    """The command's legacy ``--json`` document: printed now, or kept for the report.

    Without a report this is the 0.3 behaviour - the document on ``where``
    (stdout by default). With one, it is both kept for :meth:`Report.finish`'s
    legacy branch and recorded as a ``data`` record for the versioned format.
    """
    report = current()
    if report is None:
        print(dumps(value), file=where)
    else:
        report.payload, report.has_payload = value, True
        report.record("data", value=value)


def message(text: str, *, stderr: bool = False) -> None:
    """Human output: written to the terminal, or kept as a ``message`` record."""
    report = current()
    if report is None:
        stream = sys.stderr if stderr else sys.stdout
        stream.write(text)
        stream.flush()
    else:
        report.record("message", stream="stderr" if stderr else "stdout", text=text)


def run_cli(
    tool: str, argv: Sequence[str] | None, run: Callable[[], int], *, legacy_json: bool = False
) -> int:
    """Run a command, producing a report if ``argv`` asks for one; return its exit code.

    ``run`` parses ``argv`` and does the work; this decides the format first
    (see :func:`_selection`), captures stdout and stderr for the duration, and
    writes the finished document to the real stdout even when ``run`` raised.
    Exceptions are turned into exit codes here (see :func:`_execute`) so a
    report always has a summary. ``legacy_json`` is the tool's opt-in to the
    0.3 ``--json`` document (see :func:`flags`).
    """
    selected, legacy = _selection(sys.argv[1:] if argv is None else argv)
    # Already inside a report (a command run by another's test, or a nested
    # tool): write into that one rather than emitting a second document.
    if current() is not None or selected not in ("json", "xml"):
        return run()
    destination = cast("TextIO", _Destination(sys.stdout))
    report = Report(tool, selected, legacy=legacy and legacy_json)
    token = _ACTIVE.set(report)
    code = 1
    try:
        with (
            redirect_stdout(cast("TextIO", TextOutput(report, "stdout"))),
            redirect_stderr(cast("TextIO", TextOutput(report, "stderr"))),
        ):
            code = _execute(run, report)
    finally:
        _ACTIVE.reset(token)
        try:
            report.finish(code, destination)
        except BrokenPipeError:
            # ``| head`` closed the pipe: the document is incomplete, so the
            # run cannot count as a success, but a traceback helps nobody.
            code = 1
    return code


def _execute(run: Callable[[], int], report: Report) -> int:
    """Run the command and turn every way it can end into an exit code.

    A non-zero return with no error recorded gets a generic one, so ``ok:
    false`` always has a cause in ``records``. Ctrl-C is ``130`` (128 +
    SIGINT, :attr:`~xrdcore.cli.ExitCode.INTERRUPTED`), the shell's
    convention; any other exception is ``1``.
    """
    try:
        code = run()
        if code and not report.errors:
            report.error(RuntimeError("The command did not complete successfully"), code=code)
        return code
    except SystemExit as exc:
        return _exit(exc, report)
    except KeyboardInterrupt as exc:
        code = int(ExitCode.INTERRUPTED)
        report.error(exc, code=code, message="The command was interrupted")
        return code
    except Exception as exc:
        report.error(exc)
        return 1


def _exit(exc: SystemExit, report: Report) -> int:
    """The exit code a :class:`SystemExit` means, recorded if it is a failure.

    Python's own rules: an int is the code, ``None`` is success, and anything
    else (a message string) is printed and exits ``1``. argparse exits through
    here, with its message already captured as a stderr ``message`` record.
    """
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code and not report.errors:
        message = "Invalid command-line arguments; see the diagnostic records"
        if isinstance(exc.code, str):
            message = exc.code
        report.error(ValueError(message), code=code)
    return code
