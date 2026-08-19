from __future__ import annotations

from pdf2pdfa.native.objects import PDFName
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_cff_pdf_render import (
    _bottom_pixel,
    _cid_font,
    _dint,
    _index,
    _pdf,
    _private,
    _rect_charstring,
)


def _cid_cff_for(cid: int) -> bytes:
    header = b"\x01\x00\x04\x04"
    name = _index([b"Owned90msCID"])
    strings = b"\x00\x00"
    gsubrs = b"\x00\x00"
    charset = b"\x00" + int(cid).to_bytes(2, "big")
    private = _private(700)
    chars = _index([b"\x0e", _rect_charstring()])
    fdselect = b"\x00\x00\x00"
    top_placeholder = (
        _dint(0) + _dint(0) + _dint(0) + b"\x0c\x1e"
        + _dint(0) + b"\x0f"
        + _dint(0) + b"\x11"
        + _dint(0) + b"\x0c\x24"
        + _dint(0) + b"\x0c\x25"
    )
    prefix = header + name + _index([top_placeholder]) + strings + gsubrs
    charset_offset = len(prefix)
    private_offset = charset_offset + len(charset)
    fdarray_offset = private_offset + len(private)
    fd_dict = _dint(len(private)) + _dint(private_offset) + b"\x12"
    fd_index = _index([fd_dict])
    fdselect_offset = fdarray_offset + len(fd_index)
    chars_offset = fdselect_offset + len(fdselect)
    top = (
        _dint(0) + _dint(0) + _dint(0) + b"\x0c\x1e"
        + _dint(charset_offset) + b"\x0f"
        + _dint(chars_offset) + b"\x11"
        + _dint(fdarray_offset) + b"\x0c\x24"
        + _dint(fdselect_offset) + b"\x0c\x25"
    )
    final_prefix = header + name + _index([top]) + strings + gsubrs
    assert len(final_prefix) == len(prefix)
    return final_prefix + charset + private + fd_index + fdselect + chars


def test_90ms_rksj_horizontal_selects_cid633_and_renders_original_cff_glyph():
    font = _cid_font(_cid_cff_for(633))
    font["Encoding"] = PDFName("90ms-RKSJ-H")
    descendant = font["DescendantFonts"][0]
    descendant["W"] = [633, [700]]
    descendant["CIDSystemInfo"]["Ordering"] = b"Japan1"

    page = render_page_full(_pdf(font, bytes.fromhex("8140")), dpi=72)
    inside = _bottom_pixel(page, 25, 35)
    outside = _bottom_pixel(page, 5, 35)
    assert inside.r < 0.05 and inside.g < 0.05 and inside.b < 0.05
    assert outside.r > 0.98 and outside.g > 0.98 and outside.b > 0.98
