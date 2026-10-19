"""The DTD-free XML guard, with and without the C accelerator."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from xrdcore import safexml

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<lolz>&lol2;</lolz>"""

EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>"""


def test_ordinary_documents_parse_as_elementtree_does():
    text = (
        b'<D:multistatus xmlns:D="DAV:">'
        b"<D:response><D:href>/a</D:href></D:response></D:multistatus>"
    )
    root = safexml.fromstring(text)
    assert root.tag == "{DAV:}multistatus"
    assert root.findtext("{DAV:}response/{DAV:}href") == "/a"
    assert ET.tostring(root) == ET.tostring(ET.fromstring(text))


def test_str_input_is_accepted():
    assert safexml.fromstring("<a>b</a>").text == "b"


@pytest.mark.parametrize("document", [BILLION_LAUGHS, EXTERNAL, b"<!DOCTYPE a><a/>"])
def test_any_doctype_is_refused(document):
    with pytest.raises(safexml.UnsafeXML):
        safexml.fromstring(document)


def test_a_utf16_doctype_is_caught_too():
    with pytest.raises(safexml.UnsafeXML):
        safexml.fromstring(
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE a><a/>'.encode("utf-16")
        )


def test_unsafe_xml_is_a_value_error():
    assert issubclass(safexml.UnsafeXML, ValueError)


def test_malformed_xml_is_a_parse_error():
    with pytest.raises(ET.ParseError):
        safexml.fromstring(b"<a><b></a>")


def test_python_parser_is_guarded_at_expat(monkeypatch):
    """Without the C accelerator the guard hooks expat's own handlers."""

    class Raw:
        pass

    class PyParser:
        def __init__(self, target: object) -> None:
            self.parser = Raw()

    raw_parser = PyParser(None)
    safexml._guard_expat(raw_parser)
    for handler in ("StartDoctypeDeclHandler", "EntityDeclHandler", "ExternalEntityRefHandler"):
        with pytest.raises(safexml.UnsafeXML):
            getattr(raw_parser.parser, handler)("x")


# ---------------------------------------------------------------------------
# Ported from xrdclient's tests/test_portable_parsers.py when the module moved
# here: every encoding, both ElementTree parsers, and the trees left untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("padding", [0, 20_000])
@pytest.mark.parametrize(
    "declaration", ["<!DOCTYPE doc>", '<!DOCTYPE doc SYSTEM "urn:test:unused">']
)
def test_parser_rejects_dtds_in_every_encoding(encoding, padding, declaration):
    with pytest.raises(safexml.UnsafeXML):
        safexml.fromstring((" " * padding + declaration + "<doc/>").encode(encoding))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_normal_xml_keeps_stdlib_elements_and_namespaces(encoding):
    root = safexml.fromstring('<doc xmlns="urn:test"><item>hello</item></doc>'.encode(encoding))
    assert isinstance(root, ET.Element)
    assert root.findtext("{urn:test}item") == "hello"


def test_comment_text_is_not_mistaken_for_a_declaration():
    assert safexml.fromstring(b"<!-- <!DOCTYPE doc> --><doc/>").tag == "doc"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_internal_declarations_are_rejected_before_tree_construction(monkeypatch, encoding):
    def unexpected_tree(*args):
        raise AssertionError("a rejected document must not reach tree construction")

    monkeypatch.setattr("xrdcore.safexml.ET.fromstring", unexpected_tree)
    document = '<!DOCTYPE doc [<!ENTITY content "hello">]><doc/>'
    with pytest.raises(safexml.UnsafeXML):
        safexml.fromstring(document.encode(encoding))


@pytest.fixture(params=["accelerated", "pure-python"])
def element_tree(request, monkeypatch):
    """``fromstring`` over ElementTree's C parser, and over its Python one.

    The Python parser is what an interpreter without ``_elementtree`` uses;
    it reports a DOCTYPE late, so it gets expat-level hooks of its own.
    """
    if request.param == "pure-python":
        import xml.etree

        # Every rebinding the import makes is recorded, so the accelerated
        # module is back in place for the rest of the suite.
        monkeypatch.setitem(sys.modules, "_elementtree", None)
        monkeypatch.delitem(sys.modules, "xml.etree.ElementTree")
        monkeypatch.setattr(xml.etree, "ElementTree", ET)
        pure = importlib.import_module("xml.etree.ElementTree")
        assert not hasattr(ET.XMLParser(), "parser")  # the C one exposes no expat
        assert hasattr(pure.XMLParser(), "parser")
        monkeypatch.setattr(safexml, "ET", pure)
    return safexml.fromstring


#: Each of these declares something; each must be refused before it is used.
HOSTILE = {
    "billion laughs": (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        + "".join(f'<!ENTITY lol{n} "{f"&lol{n - 1};" * 10}">' for n in range(2, 10))
        + '<!ENTITY lol1 "&lol;&lol;">]><lolz>&lol9;</lolz>'
    ),
    "external entity": '<!DOCTYPE doc [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><doc>&xxe;</doc>',
    "parameter entity": '<!DOCTYPE doc [<!ENTITY % pe SYSTEM "http://example.invalid/x">%pe;]><doc/>',
    "external DTD": '<!DOCTYPE doc SYSTEM "http://example.invalid/doc.dtd"><doc/>',
    "public DTD": '<!DOCTYPE doc PUBLIC "-//X//Y//EN" "urn:x"><doc/>',
    "attribute default": ('<!DOCTYPE doc [<!ENTITY e "x"><!ATTLIST doc a CDATA "&e;">]><doc/>'),
}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("document", sorted(HOSTILE))
def test_declarations_are_refused_by_either_parser(element_tree, document, encoding):
    with pytest.raises(safexml.UnsafeXML, match="not allowed"):
        element_tree(HOSTILE[document].encode(encoding))


@pytest.mark.parametrize(
    "handler", ["StartDoctypeDeclHandler", "EntityDeclHandler", "ExternalEntityRefHandler"]
)
def test_the_python_parser_gets_every_expat_declaration_hook(handler):
    parser = SimpleNamespace()
    safexml._guard_expat(SimpleNamespace(parser=parser))
    with pytest.raises(safexml.UnsafeXML, match="not allowed"):
        getattr(parser, handler)("doc", None, None)


#: Documents whose trees must come out exactly as ElementTree builds them.
ORDINARY = [
    b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/a%20b</D:href>'
    b"<D:propstat><D:prop><D:getcontentlength>5</D:getcontentlength></D:prop>"
    b"</D:propstat></D:response></D:multistatus>",
    b'<?xml version="1.0" encoding="utf-8"?>\n<a xmlns="urn:x" xmlns:y="urn:y" y:k="v" k="w">'
    b"text<b>inner</b>tail<!-- note --><?pi data?><c/>end</a>",
    "<doc>caf\u00e9 &amp; &#x263A; <![CDATA[<raw>]]></doc>".encode("utf-16"),
    b'<?xml version="1.0" encoding="iso-8859-1"?><doc>\xe9</doc>',
    "<str-input>\u00e9</str-input>",
]


@pytest.mark.parametrize("document", ORDINARY)
def test_trees_are_exactly_elementtrees_own(element_tree, document):
    ours = element_tree(document)
    assert isinstance(ours, ET.Element)
    assert ET.tostring(ours) == ET.tostring(ET.fromstring(document))
    assert [(e.tag, e.attrib, e.text, e.tail) for e in ours.iter()] == [
        (e.tag, e.attrib, e.text, e.tail) for e in ET.fromstring(document).iter()
    ]


def test_a_large_listing_is_parsed_once(monkeypatch):
    """One parser per document: the declaration check is not a second pass."""
    made: list[int] = []
    real = ET.XMLParser

    def counting(**kw: Any) -> ET.XMLParser:
        made.append(1)
        return real(**kw)

    monkeypatch.setattr("xrdcore.safexml.ET.XMLParser", counting)
    monkeypatch.setattr("xrdcore.safexml.ET.fromstring", None)  # never the second pass
    body = b"<m>" + b"<r><h>/f</h></r>" * 50_000 + b"</m>"
    assert len(safexml.fromstring(body)) == 50_000
    assert made == [1]


def test_predefined_entities_and_literal_declaration_text_are_harmless():
    assert safexml.fromstring(b"<doc>A&amp;B</doc>").text == "A&B"
    assert safexml.fromstring(b"<doc><![CDATA[<!DOCTYPE doc>]]></doc>").text == "<!DOCTYPE doc>"


def test_malformed_xml_preserves_stdlib_exception_details():
    with pytest.raises(ET.ParseError) as error:
        safexml.fromstring(b"<doc>")
    assert error.value.code == 3
    assert error.value.position == (1, 5)


def test_namespace_errors_remain_stdlib_parse_errors():
    with pytest.raises(ET.ParseError, match="unbound prefix"):
        safexml.fromstring(b"<unknown:doc/>")
