from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import UnsupportedRenderingError


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


EOL = "000000000001"


def _image(
    data: bytes,
    *,
    width: int = 8,
    height: int = 1,
    k: int = 0,
    rows: int | None = None,
    columns: int | None = 8,
    black_is_1: bool = False,
    end_of_line: bool = False,
    encoded_byte_align: bool = False,
) -> PDFStream:
    parms = PDFDict({"K": k})
    if rows is not None:
        parms["Rows"] = rows
    if columns is not None:
        parms["Columns"] = columns
    if black_is_1:
        parms["BlackIs1"] = True
    if end_of_line:
        parms["EndOfLine"] = True
    if encoded_byte_align:
        parms["EncodedByteAlign"] = True
    return PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": width,
                "Height": height,
                "BitsPerComponent": 1,
                "ColorSpace": PDFName("DeviceGray"),
                "Filter": PDFName("CCITTFaxDecode"),
                "DecodeParms": parms,
            }
        ),
        data,
    )


def _document_with_image(image: PDFStream, *, page_width: int = 80, page_height: int = 10) -> bytes:
    builder = PDFBuilder(version="1.7")
    image_ref = builder.add(image)
    content = builder.add(
        PDFStream(
            PDFDict(),
            f"{page_width} 0 0 {page_height} 0 0 cm /Im Do\n".encode("ascii"),
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, page_width, page_height],
                "Resources": PDFDict({"XObject": PDFDict({"Im": image_ref})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _bottom_pixel(page, x: int, y: int = 5):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_group3_ccitt_xobject_renders_white_then_black():
    page = render_page_full(
        _document_with_image(_image(_bits("1011 011"))),
        dpi=72,
    )
    white = _bottom_pixel(page, 10)
    black = _bottom_pixel(page, 70)
    assert white.r > 0.98 and white.g > 0.98 and white.b > 0.98
    assert black.r < 0.02 and black.g < 0.02 and black.b < 0.02


def test_black_is_1_does_not_invert_visual_result_after_filter_materialization():
    ordinary = render_page_full(
        _document_with_image(_image(_bits("1011 011"), black_is_1=False)),
        dpi=72,
    )
    black_is_one = render_page_full(
        _document_with_image(_image(_bits("1011 011"), black_is_1=True)),
        dpi=72,
    )
    assert ordinary.rgb_bytes() == black_is_one.rgb_bytes()


def test_rows_zero_uses_image_height():
    encoded = _bits("10011 10011")
    data = _document_with_image(
        _image(encoded, height=2, rows=0),
        page_height=20,
    )
    doc = PDFDocument.open(data, repair=False)
    page = render_page_full(doc, dpi=72)
    assert page.width == 80 and page.height == 20
    assert all(value == 255 for value in page.rgb_bytes())


def test_optional_eol_is_accepted_when_end_of_line_is_false():
    encoded = _bits(f"{EOL} 1011 011")
    page = render_page_full(
        _document_with_image(_image(encoded, end_of_line=False)),
        dpi=72,
    )
    assert _bottom_pixel(page, 10).r > 0.98
    assert _bottom_pixel(page, 70).r < 0.02


def test_required_eol_missing_is_rejected():
    with pytest.raises(UnsupportedRenderingError, match="EOL"):
        render_page_full(
            _document_with_image(_image(_bits("1011 011"), end_of_line=True)),
            dpi=72,
        )


def test_group4_ccitt_xobject_renders_reference_rows():
    encoded = _bits("001 00110101 000101 1 1")
    page = render_page_full(
        _document_with_image(_image(encoded, height=2, rows=2, k=-1), page_height=20),
        dpi=72,
    )
    assert _bottom_pixel(page, 10, 5).r < 0.02
    assert _bottom_pixel(page, 10, 15).r < 0.02


def test_mixed_group3_k2_renders_1d_reference_then_2d_row():
    encoded = _bits(
        f"{EOL} 1 1011 011 "
        f"{EOL} 0 1 1"
    )
    page = render_page_full(
        _document_with_image(
            _image(encoded, height=2, rows=2, k=2, end_of_line=True),
            page_height=20,
        ),
        dpi=72,
    )
    for y in (5, 15):
        assert _bottom_pixel(page, 10, y).r > 0.98
        assert _bottom_pixel(page, 70, y).r < 0.02


def test_mixed_group3_k_cadence_violation_is_fail_closed_at_renderer():
    encoded = _bits(
        f"{EOL} 1 1011 011 "
        f"{EOL} 0 1 1 "
        f"{EOL} 0 1 1"
    )
    data = _document_with_image(
        _image(encoded, height=3, rows=3, k=2, end_of_line=True),
        page_height=30,
    )
    with pytest.raises(UnsupportedRenderingError, match="exceeds K=2"):
        render_page_full(data, dpi=72)


def test_columns_width_mismatch_is_fail_closed():
    data = _document_with_image(_image(_bits("10011"), columns=9))
    with pytest.raises(UnsupportedRenderingError, match="Columns 9.*Width 8"):
        render_page_full(data, dpi=72)


def test_decode_image_adapter_preserves_regular_non_ccitt_path():
    raw = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 1,
                "Height": 1,
                "BitsPerComponent": 8,
                "ColorSpace": PDFName("DeviceRGB"),
            }
        ),
        bytes([255, 0, 0]),
    )
    data = _document_with_image(raw, page_width=10, page_height=10)
    doc = PDFDocument.open(data, repair=False)
    page = render_page_full(doc, dpi=72)
    pixel = _bottom_pixel(page, 5, 5)
    assert pixel.r > 0.98 and pixel.g < 0.02 and pixel.b < 0.02
