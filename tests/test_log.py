"""Redaction and muting."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from xrdcore import log


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("https://h/f?X-Amz-Signature=abcdef0123&x=1", "abcdef0123"),
        ("root://h//f?authz=Bearer%20tok", "Bearer%20tok"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "payload"),
        ("headers={'Authorization': 'AWS4-HMAC-SHA256 Credential=AKIA/x, Signature=ff'}", "AKIA"),
        ("TransferHeaderAuthorization: Bearer abc.def", "abc.def"),
        ("token=s3cr3t", "s3cr3t"),
        ("password: hunter2", "hunter2"),
        (
            "macaroon MDAxY2xvY2F0aW9uIGh0dHBzOi8vZXhhbXBsZQo=",
            "Y2xvY2F0aW9uIGh0dHBzOi8vZXhhbXBsZQo",
        ),
        ("https://h/f?sig=xyz&cap.sym=abc", "xyz"),
    ],
)
def test_redact_removes_credentials(text, secret):
    cleaned = log.redact(text)
    assert secret not in cleaned
    assert "<redacted>" in cleaned


def test_redact_leaves_ordinary_text_alone():
    assert log.redact("copied 12 bytes to /store/f.root") == "copied 12 bytes to /store/f.root"
    assert log.redact("design=1") == "design=1"


def test_logger_redacts_after_interpolation(caplog):
    logger = log.get_logger("xrdcore.tests.interp")
    with caplog.at_level(logging.DEBUG, logger="xrdcore.tests.interp"):
        logger.debug("%s=%s", "token", "s3cr3t")
    assert "s3cr3t" not in caplog.text
    assert caplog.records[-1].args == ()


def test_get_logger_keeps_the_callers_name_and_is_idempotent():
    first = log.get_logger("gfal2")
    second = log.get_logger("gfal2")
    assert first is second and first.name == "gfal2"
    assert first.filters.count(log.FILTER) == 1


def test_a_broken_format_string_is_passed_through():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "%d", ("not a number",), None)
    assert log.FILTER.filter(record) is True


def test_mute_drops_a_band_for_a_subtree_and_unmutes(caplog):
    parent = log.get_logger("xrdcore.tests.mute")
    child = log.get_logger("xrdcore.tests.mute.child")
    sibling = log.get_logger("xrdcore.tests.muted")
    try:
        log.mute(logging.DEBUG, logging.WARNING, ["xrdcore.tests.mute"])
        with caplog.at_level(logging.DEBUG):
            child.info("hidden")
            parent.debug("hidden too")
            child.warning("shown")
            sibling.info("also shown")
        messages = [r.getMessage() for r in caplog.records]
        assert messages == ["shown", "also shown"]
    finally:
        log.mute(logging.DEBUG, logging.WARNING, [])
    assert log._muted == {}


@pytest.mark.parametrize(
    ("count", "level"),
    [(-1, logging.WARNING), (0, logging.WARNING), (1, logging.INFO), (2, logging.DEBUG), (5, 10)],
)
def test_verbosity_level_on_the_default_scale(count, level):
    assert log.verbosity_level(count) == level


def test_verbosity_level_on_a_tools_own_scale():
    scale = (logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG)
    assert [log.verbosity_level(n, scale) for n in range(5)] == [40, 30, 20, 10, 10]


def test_configure_writes_redacted_lines_to_the_stream_and_close_restores():
    logger = logging.getLogger("xrdcore.tests.configure")
    logger.setLevel(logging.ERROR)
    stream = io.StringIO()
    configured = log.configure(logging.INFO, loggers=["xrdcore.tests.configure"], stream=stream)
    try:
        assert logger.level == logging.INFO and configured.handler.level == logging.INFO
        logger.debug("hidden")
        logger.info("%s=%s", "token", "s3cr3t")
        assert stream.getvalue() == "INFO: token=<redacted>\n"
    finally:
        configured.close()
    assert logger.level == logging.ERROR
    assert configured.handler not in logger.handlers
    assert not stream.closed


def test_configure_again_replaces_its_handler_and_takes_a_formatter():
    name = "xrdcore.tests.again"
    first = log.configure(logging.WARNING, loggers=[name], stream=io.StringIO())
    stream = io.StringIO()
    second = log.configure(
        logging.DEBUG,
        loggers=[name],
        stream=stream,
        fmt=logging.Formatter("%(name)s %(message)s"),
    )
    try:
        logger = logging.getLogger(name)
        assert logger.handlers == [second.handler]
        logger.debug("once")
        assert stream.getvalue() == f"{name} once\n"
    finally:
        second.close()
        first.close()
    assert logging.getLogger(name).handlers == []


def test_configure_follows_whatever_stderr_is_at_each_record(monkeypatch):
    name = "xrdcore.tests.stderr"
    configured = log.configure(logging.WARNING, loggers=[name], fmt="%(message)s")
    try:
        for text in ("first", "second"):
            stream = io.StringIO()
            monkeypatch.setattr(sys, "stderr", stream)
            logging.getLogger(name).warning(text)
            assert stream.getvalue() == f"{text}\n"
    finally:
        configured.close()


def test_configure_defaults_to_the_root_logger():
    root = logging.getLogger()
    before = root.level
    configured = log.configure(logging.DEBUG, stream=io.StringIO())
    try:
        assert configured.handler in root.handlers and root.level == logging.DEBUG
    finally:
        configured.close()
    assert root.level == before and configured.handler not in root.handlers
