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


def _charstring(program: bytes, *, len_iv: int = 4) -> bytes:
    if len_iv == -1:
        return program
    prefix = bytes(range(1, len_iv + 1))
    return _encrypt_for_tests(prefix + program, 4330)


def _private(
    glyphs: dict[str, bytes],
    *,
    subrs: list[bytes] | None = None,
    len_iv: int = 4,
) -> bytes:
    pieces = [f"/lenIV {len_iv} def\n".encode()]
    subrs = subrs or []
    pieces.append(f"/Subrs {len(subrs)} array\n".encode())
    for index, program in enumerate(subrs):
        encrypted = _charstring(program, len_iv=len_iv)
        pieces.extend(
            [
                f"dup {index} {len(encrypted)} RD ".encode(),
                encrypted,
                b" NP\n",
            ]
        )
    pieces.append(f"/CharStrings {len(glyphs)} dict dup begin\n".encode())
    for name, program in glyphs.items():
        encrypted = _charstring(program, len_iv=len_iv)
        pieces.extend(
            [
                f"/{name} {len(encrypted)} RD ".encode(),
                encrypted,
                b" ND\n",
            ]
        )
    pieces.append(b"end\n")
    return b"".join(pieces)


def _clear() -> bytes:
    return (
        b"%!PS-AdobeFont-1.0: OwnedType1 1.0\n"
        b"/FontName /OwnedType1 def\n"
        b"/FontMatrix [0.001 0 0 0.001 0 0] readonly def\n"
        b"currentfile eexec\n"
    )


def _pfa(private: bytes) -> bytes:
    encrypted = _encrypt_for_tests(b"ABCD" + private, 55665)
    return _clear() + encrypted.hex().upper().encode("ascii") + b"\ncleartomark\n"


def _pfb(private: bytes) -> bytes:
    encrypted = _encrypt_for_tests(b"ABCD" + private, 55665)

    def segment(kind: int, payload: bytes) -> bytes:
        return b"\x80" + bytes([kind]) + len(payload).to_bytes(4, "little") + payload

    return (
        segment(1, _clear())
        + segment(2, encrypted)
        + segment(1, b"\ncleartomark\n")
        + b"\x80\x03"
    )


def _notdef() -> bytes:
    return _num(0) + _num(600) + b"\x0d\x0e"


def _rectangle() -> bytes:
    return b"".join(
        [
            _num(0), _num(600), b"\x0d",  # hsbw
            _num(100), _num(100), b"\x15",  # rmoveto
            _num(500), _num(0), b"\x05",
            _num(0), _num(700), b"\x05",
            _num(-500), _num(0), b"\x05",
            _num(0), _num(-700), b"\x05",
            b"\x09\x0e",
        ]
    )


def _font_bytes(*, pfb: bool = False, len_iv: int = 4, glyph=None, subrs=None) -> bytes:
    private = _private(
        {".notdef": _notdef(), "A": glyph or _rectangle()},
        subrs=subrs,
        len_iv=len_iv,
    )
    return _pfb(private) if pfb else _pfa(private)


def test_pfa_ascii_hex_eexec_parses_font_metadata_and_glyphs():
    font = Type1Font(_font_bytes())
    assert font.font_name == "OwnedType1"
    assert font.font_matrix == (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
    assert font.len_iv == 4
    assert set(font.glyph_names) == {".notdef", "A"}


def test_pfb_binary_segments_parse_same_program_as_pfa():
    pfa = Type1Font(_font_bytes())
    pfb = Type1Font(_font_bytes(pfb=True))
    assert pfb.font_name == pfa.font_name
    assert pfb.font_matrix == pfa.font_matrix
    assert pfb.outline("A") == pfa.outline("A")


def test_rectangle_charstring_emits_owned_path_and_width():
    outline = Type1Font(_font_bytes()).outline("A")
    assert outline.width_x == 600
    assert outline.width_y == 0
    assert [item.operator for item in outline.commands] == ["M", "L", "L", "L", "L", "Z"]
    assert outline.commands[0].values == (100, 100)
    assert outline.commands[2].values == (600, 800)


def test_missing_glyph_falls_back_to_notdef():
    font = Type1Font(_font_bytes())
    assert font.outline("DoesNotExist") == font.outline(".notdef")


def test_leniv_minus_one_accepts_plain_charstrings():
    font = Type1Font(_font_bytes(len_iv=-1))
    assert font.len_iv == -1
    assert font.outline("A").width_x == 600


def test_rrcurveto_hvcurveto_and_vhcurveto_are_geometric():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(100), _num(100), b"\x15",
            _num(20), _num(0), _num(20), _num(20), _num(20), _num(0), b"\x08",
            _num(20), _num(20), _num(20), _num(20), b"\x1f",
            _num(20), _num(20), _num(20), _num(20), b"\x1e",
            b"\x0e",
        ]
    )
    commands = Type1Font(_font_bytes(glyph=glyph)).outline("A").commands
    curves = [command for command in commands if command.operator == "C"]
    assert len(curves) == 3
    assert curves[0].values[-2:] == (160, 120)
    assert curves[1].values[-2:] == (200, 160)
    assert curves[2].values[-2:] == (240, 200)


def test_local_subr_uses_shared_current_point_and_returns():
    subr = _num(200) + _num(0) + b"\x05\x0b"
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(100), _num(100), b"\x15",
            _num(0), b"\x0a",
            _num(0), _num(200), b"\x05",
            b"\x0e",
        ]
    )
    outline = Type1Font(_font_bytes(glyph=glyph, subrs=[subr])).outline("A")
    assert [command.values for command in outline.commands if command.operator == "L"] == [
        (300, 100),
        (300, 300),
    ]


def test_div_operator_produces_fractional_coordinate():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(1), _num(2), b"\x0c\x0c", _num(100), b"\x15",
            b"\x0e",
        ]
    )
    outline = Type1Font(_font_bytes(glyph=glyph)).outline("A")
    assert outline.commands[0].values == (0.5, 100)


def test_sbw_sets_two_dimensional_advance_without_painting():
    glyph = b"".join(
        [
            _num(10), _num(20), _num(500), _num(30), b"\x0c\x07",
            _num(0), _num(0), b"\x15",
            b"\x0e",
        ]
    )
    outline = Type1Font(_font_bytes(glyph=glyph)).outline("A")
    assert outline.width_x == 500
    assert outline.width_y == 30
    assert outline.commands[0].values == (10, 20)


def test_seac_remains_explicit_fail_closed():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(0), _num(10), _num(20), _num(65), _num(39), b"\x0c\x06",
        ]
    )
    with pytest.raises(UnsupportedType1Error, match="seac"):
        Type1Font(_font_bytes(glyph=glyph)).outline("A")


def test_callothersubr_flex_remains_explicit_fail_closed():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _num(0), _num(1), b"\x0c\x10",
            b"\x0e",
        ]
    )
    with pytest.raises(UnsupportedType1Error, match="OtherSubrs"):
        Type1Font(_font_bytes(glyph=glyph)).outline("A")


def test_malformed_pfb_segment_length_is_rejected():
    data = b"\x80\x01" + (1000).to_bytes(4, "little") + b"short"
    with pytest.raises(Type1Error, match="segment length"):
        Type1Font(data)


def test_missing_eexec_is_rejected():
    with pytest.raises(Type1Error, match="no eexec"):
        Type1Font(b"%!PS-AdobeFont-1.0\n/FontName /Broken def\n")


def test_missing_notdef_is_rejected():
    private = _private({"A": _rectangle()})
    with pytest.raises(Type1Error, match="no /.notdef"):
        Type1Font(_pfa(private))


def test_truncated_charstring_payload_is_rejected():
    private = b"/lenIV 4 def\n/Subrs 0 array\n/CharStrings 1 dict\n/.notdef 99 RD abc"
    with pytest.raises(Type1Error, match="truncated Type1 CharString"):
        Type1Font(_pfa(private))


def test_recursive_subr_depth_is_bounded():
    subr = _num(0) + b"\x0a\x0b"
    glyph = _num(0) + _num(600) + b"\x0d" + _num(0) + b"\x0a\x0e"
    with pytest.raises(Type1Error, match="recursion exceeds"):
        Type1Font(_font_bytes(glyph=glyph, subrs=[subr])).outline("A")
