from __future__ import annotations

import pytest

from pdf2pdfa.native.type1_core import Type1Error, Type1Font

from tests.native.test_type1_core import _font_bytes, _num


def test_callsubr_preserves_operands_below_subr_index_for_subroutine():
    # Caller leaves dx/dy below the Subr index. The subroutine consumes those
    # operands with rlineto and returns with the shared stack empty.
    subr = b"\x05\x0b"  # rlineto, return
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(100), _num(100), b"\x15",
            _num(200), _num(50), _num(0), b"\x0a",
            b"\x0e",
        ]
    )
    outline = Type1Font(_font_bytes(glyph=glyph, subrs=[subr])).outline("A")
    lines = [command for command in outline.commands if command.operator == "L"]
    assert len(lines) == 1
    assert lines[0].values == (300, 150)


def test_large_32bit_integer_is_rejected_outside_immediate_div_context():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(40000), _num(0), b"\x15",
            b"\x0e",
        ]
    )
    with pytest.raises(Type1Error, match="only valid immediately before div"):
        Type1Font(_font_bytes(glyph=glyph)).outline("A")


def test_large_32bit_integer_is_allowed_as_div_numerator():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(40000), _num(400), b"\x0c\x0c",
            _num(100), b"\x15",
            b"\x0e",
        ]
    )
    outline = Type1Font(_font_bytes(glyph=glyph)).outline("A")
    assert outline.commands[0].values == (100, 100)


def test_large_32bit_div_denominator_must_be_regular_integer():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(40000), _num(50000), b"\x0c\x0c",
            _num(100), b"\x15",
            b"\x0e",
        ]
    )
    with pytest.raises(Type1Error, match="denominator shall be a regular integer"):
        Type1Font(_font_bytes(glyph=glyph)).outline("A")
