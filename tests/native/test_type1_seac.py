from __future__ import annotations

import pytest

from pdf2pdfa.native.type1 import Type1Error
from pdf2pdfa.native.type1_seac import SeacType1Font

from tests.native.test_type1_core import _notdef, _num, _pfa, _private


def _box(*, sbx: int, width: int, x: int, y: int, w: int, h: int) -> bytes:
    return b"".join(
        [
            _num(sbx), _num(width), b"\x0d",
            _num(x), _num(y), b"\x15",
            _num(w), _num(0), b"\x05",
            _num(0), _num(h), b"\x05",
            _num(-w), _num(0), b"\x05",
            _num(0), _num(-h), b"\x05",
            b"\x09\x0e",
        ]
    )


def _seac(*, lsb: int = 50, width: int = 700, asb: int = 30, adx: int = 200, ady: int = 50, bchar: int = 65, achar: int = 194) -> bytes:
    return b"".join(
        [
            _num(lsb), _num(width), b"\x0d",
            _num(asb), _num(adx), _num(ady), _num(bchar), _num(achar),
            b"\x0c\x06",
        ]
    )


def _font(*, composite: bytes | None = None, include_accent: bool = True, nested_base: bool = False) -> bytes:
    base = _seac(bchar=65, achar=194) if nested_base else _box(
        sbx=0, width=600, x=100, y=100, w=300, h=400
    )
    glyphs = {
        ".notdef": _notdef(),
        "A": base,
        "Aacute": composite or _seac(),
    }
    if include_accent:
        glyphs["acute"] = _box(sbx=30, width=200, x=10, y=500, w=100, h=100)
    return _pfa(_private(glyphs))


def test_seac_composes_base_and_accent_with_standardencoding_offset():
    outline = SeacType1Font(_font()).outline("Aacute")
    moves = [cmd.values for cmd in outline.commands if cmd.operator == "M"]
    assert outline.width_x == 700
    assert moves[0] == (100, 100)  # base glyph unchanged
    # Accent raw first point is (sbx 30 + rmoveto x 10, y 500) = (40, 500).
    # seac shift is composite_lsb 50 + adx 200 - asb 30 = 220, dy=50.
    assert moves[1] == (260, 550)


def test_seac_uses_composite_width_not_base_or_accent_width():
    outline = SeacType1Font(_font(composite=_seac(width=777))).outline("Aacute")
    assert outline.width_x == 777


def test_seac_standardencoding_code_must_be_defined():
    with pytest.raises(Type1Error, match="undefined Adobe StandardEncoding"):
        SeacType1Font(_font(composite=_seac(bchar=0))).outline("Aacute")


def test_seac_components_must_exist_and_do_not_fallback_to_notdef():
    with pytest.raises(Type1Error, match="missing glyph /acute"):
        SeacType1Font(_font(include_accent=False)).outline("Aacute")


def test_nested_seac_is_rejected():
    with pytest.raises(Type1Error, match="nested Type1 seac"):
        SeacType1Font(_font(nested_base=True)).outline("Aacute")


def test_seac_after_painted_geometry_is_rejected():
    malformed = b"".join(
        [
            _num(0), _num(700), b"\x0d",
            _num(10), _num(10), b"\x15",
            _num(20), _num(0), b"\x05",
            _num(30), _num(200), _num(50), _num(65), _num(194), b"\x0c\x06",
        ]
    )
    with pytest.raises(Type1Error, match="shall not follow painted"):
        SeacType1Font(_font(composite=malformed)).outline("Aacute")
