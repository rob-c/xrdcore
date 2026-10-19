"""DTD-free XML loading using only Python's built-in parsers, in one pass.

A document type declaration is the only place XML can declare an entity, so
refusing every ``<!DOCTYPE`` refuses entity expansion (the "billion laughs")
and external entities with it. The refusal happens *while* the tree is
built, not in a separate pass first: a ``PROPFIND`` answer may be tens of
megabytes, and parsing it twice cost a third of the time it took to read.

The tree is ElementTree's own, built by its C parser, so element names,
namespaces, text and error reports are exactly what
:func:`xml.etree.ElementTree.fromstring` gives. The hook is the target's
``doctype`` callback, which the C parser invokes from expat's start-of-DOCTYPE
event - before the internal subset, and so before any declaration in it, is
read. Without the C accelerator, ElementTree's Python parser reports a
DOCTYPE only once it has been read in full, so there the expat parser it
exposes gets the same start-of-declaration hooks directly. Either way the
check is expat's, so it is encoding-aware: a UTF-16 DOCTYPE is caught as
surely as an ASCII one. No external resource is ever read.

Everything a grid client parses as XML goes through here: WebDAV
``PROPFIND`` multistatus, S3 listings and errors, Metalink, SRM SOAP, BDII
answers, A-REX job descriptions and VOMS REST replies.
"""

from __future__ import annotations

from typing import NoReturn
from xml.etree import ElementTree as ET

__all__ = ["UnsafeXML", "fromstring"]


class UnsafeXML(ValueError):
    """XML that uses document types, declared entities or external references."""


def _reject_declaration(*arguments: object) -> NoReturn:
    """Raise :class:`UnsafeXML`; any signature, so it can stand in for any expat handler."""
    raise UnsafeXML("XML document type and entity declarations are not allowed")


class _Builder(ET.TreeBuilder):
    """ElementTree's tree builder, refusing a document type as it starts."""

    def doctype(self, name: str, pubid: str | None, system: str | None) -> NoReturn:
        """Raise :class:`UnsafeXML` the moment a ``<!DOCTYPE`` is seen.

        A DOCTYPE is where entity declarations live - the vehicle for
        billion-laughs expansion and external-entity reads - and no
        WebDAV/S3/Metalink/SOAP response a storage client parses needs one.
        """
        _reject_declaration(name, pubid, system)


def _guard_expat(parser: object) -> None:
    """Refuse declarations at expat itself, where the parser exposes it.

    Only ElementTree's pure-Python parser does - the one used when the C
    accelerator is missing - and it is also the one that reports a DOCTYPE
    late, so it is exactly the one that needs this. Any parser is accepted
    and looked at by attribute, because which one ``ET.XMLParser`` is depends
    on the interpreter.
    """
    raw = getattr(parser, "parser", None)
    if raw is not None:
        raw.StartDoctypeDeclHandler = _reject_declaration
        raw.EntityDeclHandler = _reject_declaration
        raw.ExternalEntityRefHandler = _reject_declaration


def fromstring(data: bytes | str) -> ET.Element:
    """A standard-library element tree of ``data``, which may not declare anything.

    Raises :class:`UnsafeXML` for a document type or entity declaration and
    :class:`xml.etree.ElementTree.ParseError` for anything malformed.
    """
    parser = ET.XMLParser(target=_Builder())
    _guard_expat(parser)
    parser.feed(data)
    root: ET.Element = parser.close()
    return root
