from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.cff_render import CFFTextPageRendererMixin
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.page_render import UnsupportedRenderingError


def _index(items: list[bytes]) -> bytes:
    if not items:
        return b"\x00\x00"
    offsets = [1]
    total = 1
    for item in items:
        total += len(item)
        offsets.append(total)
    return (
        len(items).to_bytes(2, "big")
        + b"\x02"
        + b"".join(value.to_bytes(2, "big") for value in offsets)
        + b"".join(items)
    )


def _dint(value: int) -> bytes:
    return b"\x1d" + int(value).to_bytes(4, "big", signed=True)


def _csnum(value: int) -> bytes:
    if -107 <= value <= 107:
        return bytes([value + 139])
    return b"\x1c" + int(value).to_bytes(2, "big", signed=True)


def _private(default_width: int = 600) -> bytes:
    return _dint(default_width) + b"\x14" + _dint(0) + b"\x15"


def _rect_charstring() -> bytes:
    return (
        _csnum(100) + _csnum(100) + b"\x15"
        + _csnum(0) + _csnum(700) + b"\x05"
        + _csnum(500) + _csnum(0) + b"\x05"
        + _csnum(0) + _csnum(-700) + b"\x05"
        + _csnum(-500) + _csnum(0) + b"\x05"
        + b"\x0e"
    )


def _simple_cff(glyph_name: bytes = b"A") -> bytes:
    # Put the requested name in String INDEX so the fixture does not rely on a
    # hidden SID lookup table. Custom-string SID 391 selects it.
    header = b"\x01\x00\x04\x04"
    name = _index([b"OwnedPDFCFF"])
    strings = _index([glyph_name])
    gsubrs = b"\x00\x00"
    charset = b"\x00" + (391).to_bytes(2, "big")
    private = _private()
    chars = _index([b"\x0e", _rect_charstring()])
    top_placeholder = (
        _dint(0) + b"\x0f"
        + _dint(0) + b"\x11"
        + _dint(len(private)) + _dint(0) + b"\x12"
    )
    prefix = header + name + _index([top_placeholder]) + strings + gsubrs
    charset_offset = len(prefix)
    private_offset = charset_offset + len(charset)
    chars_offset = private_offset + len(private)
    top = (
        _dint(charset_offset) + b"\x0f"
        + _dint(chars_offset) + b"\x11"
        + _dint(len(private)) + _dint(private_offset) + b"\x12"
    )
    prefix = header + name + _index([top]) + strings + gsubrs
    return prefix + charset + private + chars


def _cid_cff(*, fd_matrix: bool = False) -> bytes:
    header = b"\x01\x00\x04\x04"
    name = _index([b"OwnedCIDPDF"])
    strings = b"\x00\x00"
    gsubrs = b"\x00\x00"
    charset = b"\x00" + (100).to_bytes(2, "big")
    private = _private(700)
    chars = _index([b"\x0e", _rect_charstring()])
    fdselect = b"\x00\x00\x00"
    matrix = (
        _dint(2) + _dint(0) + _dint(0) + _dint(2) + _dint(0) + _dint(0) + b"\x0c\x07"
        if fd_matrix else b""
    )
    fd_placeholder = _dint(len(private)) + _dint(0) + b"\x12" + matrix
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
    fd_dict = _dint(len(private)) + _dint(private_offset) + b"\x12" + matrix
    fd_index = _index([fd_dict])
    assert len(fd_index) == len(_index([fd_placeholder]))
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


def _simple_font(cff: bytes, *, encoding, first: int = 65, last: int = 65) -> PDFDict:
    stream = PDFStream(PDFDict({"Subtype": PDFName("Type1C")}), cff)
    descriptor = PDFDict(
        {
            "Type": PDFName("FontDescriptor"),
            "FontName": PDFName("OwnedPDFCFF"),
            "Flags": 32,
            "FontBBox": [0, 0, 700, 800],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
            "MissingWidth": 600,
            "FontFile3": stream,
        }
    )
    return PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type1"),
            "BaseFont": PDFName("OwnedPDFCFF"),
            "FirstChar": first,
            "LastChar": last,
            "Widths": [600] * (last - first + 1),
            "Encoding": encoding,
            "FontDescriptor": descriptor,
        }
    )


def _cid_font(cff: bytes) -> PDFDict:
    stream = PDFStream(PDFDict({"Subtype": PDFName("CIDFontType0C")}), cff)
    descriptor = PDFDict(
        {
            "Type": PDFName("FontDescriptor"),
            "FontName": PDFName("OwnedCIDPDF"),
            "Flags": 4,
            "FontBBox": [0, 0, 700, 800],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
            "FontFile3": stream,
        }
    )
    descendant = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("CIDFontType0"),
            "BaseFont": PDFName("OwnedCIDPDF"),
            "CIDSystemInfo": PDFDict(
                {"Registry": b"Adobe", "Ordering": b"Identity", "Supplement": 0}
            ),
            "FontDescriptor": descriptor,
            "DW": 700,
            "W": [100, [700]],
        }
    )
    return PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type0"),
            "BaseFont": PDFName("OwnedCIDPDF"),
            "Encoding": PDFName("Identity-H"),
            "DescendantFonts": [descendant],
        }
    )


def _pdf(font: PDFDict, text_bytes: bytes, *, extra_content: bytes = b"", render_mode: int = 0) -> bytes:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(font)
    content = (
        b"BT /F 60 Tf 1 0 0 1 10 10 Tm "
        + str(render_mode).encode("ascii") + b" Tr <" + text_bytes.hex().encode("ascii") + b"> Tj ET\n"
        + extra_content
    )
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F": font_ref})}),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_full_renderer_mro_includes_cff_text_bridge():
    assert CFFTextPageRendererMixin in FullOwnedPageRenderer.__mro__


def test_simple_type1c_winansi_renders_original_cff_outline():
    font = _simple_font(_simple_cff(b"A"), encoding=PDFName("WinAnsiEncoding"))
    page = render_page_full(_pdf(font, b"A"), dpi=72)
    inside = _bottom_pixel(page, 25, 35)
    outside = _bottom_pixel(page, 5, 35)
    assert inside.r < 0.05 and inside.g < 0.05 and inside.b < 0.05
    assert outside.r > 0.98 and outside.g > 0.98 and outside.b > 0.98


def test_simple_type1c_winansi_extended_name_maps_to_cff_glyph():
    font = _simple_font(
        _simple_cff(b"Aacute"),
        encoding=PDFName("WinAnsiEncoding"),
        first=0xC1,
        last=0xC1,
    )
    page = render_page_full(_pdf(font, bytes([0xC1])), dpi=72)
    assert _bottom_pixel(page, 25, 35).r < 0.05


def test_simple_type1c_differences_override_base_encoding():
    encoding = PDFDict(
        {
            "BaseEncoding": PDFName("WinAnsiEncoding"),
            "Differences": [65, PDFName("ownedGlyph")],
        }
    )
    font = _simple_font(_simple_cff(b"ownedGlyph"), encoding=encoding)
    page = render_page_full(_pdf(font, b"A"), dpi=72)
    assert _bottom_pixel(page, 25, 35).r < 0.05


def test_simple_type1c_text_clip_mode_clips_following_paint():
    font = _simple_font(_simple_cff(b"A"), encoding=PDFName("WinAnsiEncoding"))
    extra = b"1 0 0 rg 0 0 100 100 re f\n"
    page = render_page_full(_pdf(font, b"A", extra_content=extra, render_mode=7), dpi=72)
    inside = _bottom_pixel(page, 25, 35)
    outside = _bottom_pixel(page, 80, 80)
    assert inside.r > 0.95 and inside.g < 0.05 and inside.b < 0.05
    assert outside.r > 0.98 and outside.g > 0.98 and outside.b > 0.98


def test_cidfonttype0c_identity_h_maps_cid_through_cff_charset():
    font = _cid_font(_cid_cff(fd_matrix=False))
    page = render_page_full(_pdf(font, (100).to_bytes(2, "big")), dpi=72)
    assert _bottom_pixel(page, 25, 35).r < 0.05


def test_cidfonttype0c_per_fd_fontmatrix_remains_fail_closed():
    font = _cid_font(_cid_cff(fd_matrix=True))
    with pytest.raises(UnsupportedRenderingError, match="per-FD FontMatrix"):
        render_page_full(_pdf(font, (100).to_bytes(2, "big")), dpi=72)
