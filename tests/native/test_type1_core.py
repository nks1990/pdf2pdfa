from __future__ import annotations

import pytest

from pdf2pdfa.native.type1 import (
    Type1Error,
    Type1Font,
    UnsupportedType1Error,
    _encrypt_for_tests,
)


def _num(value: int) -> bytes:
    if -107 <= value <= 107:
        return bytes([value + 139])
    if 108 <= value <= 1131:
        value -= 108
        return bytes([247 + value // 256, value % 256])
    if -1131 <= value <= -108:
        value = -value - 108
        return bytes([251 + value // 256, value % 256])
    return b"\xff" + int(value).to_bytes(4, "big", signed=True)


def _encrypted_charstring(program: bytes, len_iv: int) -> bytes:
    if len_iv == -1:
        return program
    return _encrypt_for_tests(bytes(range(1, len_iv + 1)) + program, 4330)


def _private(glyphs: dict[str, bytes], *, subrs=(), len_iv: int = 4) -> bytes:
    out = [f"/lenIV {len_iv} def\n/Subrs {len(subrs)} array\n".encode()]
    for index, program in enumerate(subrs):
        payload = _encrypted_charstring(program, len_iv)
        out += [f"dup {index} {len(payload)} RD ".encode(), payload, b" NP\n"]
    out.append(f"/CharStrings {len(glyphs)} dict dup begin\n".encode())
    for name, program in glyphs.items():
        payload = _encrypted_charstring(program, len_iv)
        out += [f"/{name} {len(payload)} RD ".encode(), payload, b" ND\n"]
    out.append(b"end\n")
    return b"".join(out)


def _clear() -> bytes:
    return (
        b"%!PS-AdobeFont-1.0: OwnedType1 1.0\n"
        b"/FontName /OwnedType1 def\n"
        b"/FontMatrix [0.001 0 0 0.001 0 0] readonly def\n"
        b"currentfile eexec\n"
    )


def _pfa(private: bytes) -> bytes:
    encrypted = _encrypt_for_tests(b"ABCD" + private, 55665)
    return _clear() + encrypted.hex().upper().encode() + b"\ncleartomark\n"


def _pfb(private: bytes) -> bytes:
    encrypted = _encrypt_for_tests(b"ABCD" + private, 55665)
    def seg(kind: int, payload: bytes) -> bytes:
        return b"\x80" + bytes([kind]) + len(payload).to_bytes(4, "little") + payload
    return seg(1, _clear()) + seg(2, encrypted) + seg(1, b"cleartomark\n") + b"\x80\x03"


def _notdef() -> bytes:
    return _num(0) + _num(600) + b"\x0d\x0e"


def _rect() -> bytes:
    return b"".join([
        _num(0), _num(600), b"\x0d",
        _num(100), _num(100), b"\x15",
        _num(500), _num(0), b"\x05",
        _num(0), _num(700), b"\x05",
        _num(-500), _num(0), b"\x05",
        _num(0), _num(-700), b"\x05",
        b"\x09\x0e",
    ])


def _font(*, pfb=False, len_iv=4, glyph=None, subrs=()) -> bytes:
    private = _private({".notdef": _notdef(), "A": glyph or _rect()}, subrs=subrs, len_iv=len_iv)
    return _pfb(private) if pfb else _pfa(private)


def test_pfa_and_pfb_decode_to_same_owned_outline():
    pfa = Type1Font(_font())
    pfb = Type1Font(_font(pfb=True))
    assert pfa.font_name == pfb.font_name == "OwnedType1"
    assert pfa.font_matrix == (0.001, 0, 0, 0.001, 0, 0)
    assert pfa.outline("A") == pfb.outline("A")


def test_rectangle_width_path_and_notdef_fallback():
    font = Type1Font(_font())
    outline = font.outline("A")
    assert outline.width_x == 600 and outline.width_y == 0
    assert [cmd.operator for cmd in outline.commands] == ["M", "L", "L", "L", "L", "Z"]
    assert outline.commands[0].values == (100, 100)
    assert font.outline("missing") == font.outline(".notdef")


def test_leniv_minus_one_uses_plain_charstrings():
    font = Type1Font(_font(len_iv=-1))
    assert font.len_iv == -1
    assert font.outline("A").width_x == 600


def test_curve_operators_are_geometric():
    glyph = b"".join([
        _num(0), _num(600), b"\x0d", _num(100), _num(100), b"\x15",
        _num(20), _num(0), _num(20), _num(20), _num(20), _num(0), b"\x08",
        _num(20), _num(20), _num(20), _num(20), b"\x1f",
        _num(20), _num(20), _num(20), _num(20), b"\x1e", b"\x0e",
    ])
    curves = [c for c in Type1Font(_font(glyph=glyph)).outline("A").commands if c.operator == "C"]
    assert [c.values[-2:] for c in curves] == [(160, 120), (200, 160), (240, 200)]


def test_subr_can_consume_operands_left_below_subr_index():
    subr = b"\x05\x0b"  # rlineto return; consumes caller dx/dy
    glyph = b"".join([
        _num(0), _num(600), b"\x0d", _num(100), _num(100), b"\x15",
        _num(200), _num(50), _num(0), b"\x0a", b"\x0e",
    ])
    outline = Type1Font(_font(glyph=glyph, subrs=[subr])).outline("A")
    assert [c.values for c in outline.commands if c.operator == "L"] == [(300, 150)]


def test_subr_recursion_is_bounded():
    subr = _num(0) + b"\x0a\x0b"
    glyph = _num(0) + _num(600) + b"\x0d" + _num(0) + b"\x0a\x0e"
    with pytest.raises(Type1Error, match="recursion exceeds"):
        Type1Font(_font(glyph=glyph, subrs=[subr])).outline("A")


def test_regular_div_and_large_integer_div_semantics():
    ordinary = b"".join([
        _num(0), _num(600), b"\x0d", _num(1), _num(2), b"\x0c\x0c", _num(100), b"\x15\x0e",
    ])
    assert Type1Font(_font(glyph=ordinary)).outline("A").commands[0].values == (0.5, 100)

    large = b"".join([
        _num(0), _num(600), b"\x0d", _num(40000), _num(400), b"\x0c\x0c", _num(100), b"\x15\x0e",
    ])
    assert Type1Font(_font(glyph=large)).outline("A").commands[0].values == (100, 100)


def test_large_integer_outside_immediate_div_is_rejected():
    glyph = _num(0)+_num(600)+b"\x0d"+_num(40000)+_num(0)+b"\x15\x0e"
    with pytest.raises(Type1Error, match="only valid immediately before div"):
        Type1Font(_font(glyph=glyph)).outline("A")


def test_large_integer_denominator_must_be_regular_integer():
    glyph = _num(0)+_num(600)+b"\x0d"+_num(40000)+_num(50000)+b"\x0c\x0c"+_num(100)+b"\x15\x0e"
    with pytest.raises(Type1Error, match="denominator shall be a regular integer"):
        Type1Font(_font(glyph=glyph)).outline("A")


def test_sbw_preserves_two_dimensional_width():
    glyph = _num(10)+_num(20)+_num(500)+_num(30)+b"\x0c\x07"+_num(0)+_num(0)+b"\x15\x0e"
    outline = Type1Font(_font(glyph=glyph)).outline("A")
    assert (outline.width_x, outline.width_y) == (500, 30)
    assert outline.commands[0].values == (10, 20)


def test_seac_and_other_subrs_are_explicit_blockers():
    seac = _num(0)+_num(600)+b"\x0d"+_num(0)+_num(10)+_num(20)+_num(65)+_num(39)+b"\x0c\x06"
    with pytest.raises(UnsupportedType1Error, match="seac"):
        Type1Font(_font(glyph=seac)).outline("A")
    other = _num(0)+_num(600)+b"\x0d"+_num(0)+_num(1)+b"\x0c\x10\x0e"
    with pytest.raises(UnsupportedType1Error, match="OtherSubrs"):
        Type1Font(_font(glyph=other)).outline("A")


def test_corrupt_envelopes_and_charstrings_fail_closed():
    with pytest.raises(Type1Error, match="segment length"):
        Type1Font(b"\x80\x01" + (1000).to_bytes(4, "little") + b"short")
    with pytest.raises(Type1Error, match="no eexec"):
        Type1Font(b"%!PS-AdobeFont-1.0\n/FontName /Broken def\n")
    with pytest.raises(Type1Error, match="no /.notdef"):
        Type1Font(_pfa(_private({"A": _rect()})))
    broken = b"/lenIV 4 def\n/Subrs 0 array\n/CharStrings 1 dict\n/.notdef 99 RD abc"
    with pytest.raises(Type1Error, match="truncated Type1 CharString"):
        Type1Font(_pfa(broken))
