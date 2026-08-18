from __future__ import annotations

from decimal import Decimal

from pdf2pdfa.native.content import (
    ContentInstruction,
    InlineImage,
    parse_content_stream,
    serialize_content_stream,
)
from pdf2pdfa.native.objects import PDFName


def test_parse_graphics_text_and_resource_operators():
    data = b"q 1 0 0 1 10 20 cm /F1 12 Tf 0.1 0.2 0.3 rg (Hi) Tj Q"
    items = parse_content_stream(data)
    assert [item.operator for item in items] == ["q", "cm", "Tf", "rg", "Tj", "Q"]
    tf = items[2]
    assert isinstance(tf, ContentInstruction)
    assert tf.operands[0] == PDFName("F1")
    assert tf.operands[1] == 12
    rgb = items[3]
    assert rgb.operands == (Decimal("0.1"), Decimal("0.2"), Decimal("0.3"))


def test_inline_image_parser_does_not_stop_on_embedded_ei_bytes():
    data = b"q BI /W 2 /H 1 /BPC 8 /CS /G ID abcEIx yz EI Q"
    items = parse_content_stream(data)
    assert len(items) == 3
    image = items[1]
    assert isinstance(image, InlineImage)
    assert image.dictionary["W"] == 2
    assert image.dictionary["CS"] == PDFName("G")
    assert image.data == b"abcEIx yz"


def test_content_roundtrip_keeps_semantics():
    source = b"/F1 10 Tf 0 0 m 10 10 l S (abc) Tj"
    first = parse_content_stream(source)
    encoded = serialize_content_stream(first)
    second = parse_content_stream(encoded)
    assert [item.operator for item in second] == [item.operator for item in first]
    for left, right in zip(first, second):
        assert type(left) is type(right)
        if isinstance(left, ContentInstruction) and isinstance(right, ContentInstruction):
            assert left.operands == right.operands
