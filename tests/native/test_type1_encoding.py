from __future__ import annotations

from pdf2pdfa.native.font_encoding import base_encoding
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.type1 import _encrypt_for_tests

from tests.native.test_type1_core import _notdef, _private, _rect
from tests.native.test_type1_pdf_render import _pdf, _type1_font


def _program_with_encoding(encoding_program: bytes) -> bytes:
    clear = (
        b"%!PS-AdobeFont-1.0: OwnedType1 1.0\n"
        b"/FontName /OwnedType1 def\n"
        b"/FontMatrix [0.001 0 0 0.001 0 0] readonly def\n"
        b"/Encoding " + encoding_program + b"\n"
        b"currentfile eexec\n"
    )
    private = _private({".notdef": _notdef(), "A": _rect()})
    encrypted = _encrypt_for_tests(b"ABCD" + private, 55665)
    return clear + encrypted.hex().upper().encode("ascii") + b"\ncleartomark\n"


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_adobe_standard_encoding_is_complete_at_non_ascii_and_quote_positions():
    mapping = base_encoding("StandardEncoding")
    assert mapping[39] == "quoteright"
    assert mapping[96] == "quoteleft"
    assert mapping[161] == "exclamdown"
    assert mapping[208] == "emdash"
    assert mapping[225] == "AE"
    assert mapping[251] == "germandbls"


def test_pdf_type1_without_encoding_uses_embedded_standard_encoding():
    program = _program_with_encoding(b"StandardEncoding def")
    font = _type1_font(program)
    font.pop("Encoding", None)
    page = render_page_full(
        _pdf(font, b"BT /F 60 Tf 1 0 0 1 10 10 Tm <41> Tj ET\n"),
        dpi=72,
    )
    assert _bottom_pixel(page, 25, 35).r < 0.05


def test_differences_without_base_encoding_inherit_type1_builtin_encoding():
    program = _program_with_encoding(b"StandardEncoding def")
    font = _type1_font(
        program,
        encoding=PDFDict({"Differences": [66, PDFName("A")]}),
        first=65,
        last=66,
    )
    page = render_page_full(
        _pdf(font, b"BT /F 60 Tf 1 0 0 1 10 10 Tm <4142> Tj ET\n"),
        dpi=72,
    )
    # Code 65 comes from built-in StandardEncoding; code 66 comes from the
    # PDF Differences array. Both must remain mapped.
    assert _bottom_pixel(page, 25, 35).r < 0.05
    assert _bottom_pixel(page, 60, 35).r < 0.05


def test_custom_builtin_256_array_is_used_when_pdf_encoding_is_absent():
    program = _program_with_encoding(
        b"256 array 0 1 255 {1 index exch /.notdef put} for "
        b"dup 65 /A put readonly def"
    )
    font = _type1_font(program)
    font.pop("Encoding", None)
    page = render_page_full(
        _pdf(font, b"BT /F 60 Tf 1 0 0 1 10 10 Tm <41> Tj ET\n"),
        dpi=72,
    )
    assert _bottom_pixel(page, 25, 35).r < 0.05
