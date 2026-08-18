from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full


def _pdf(*, horizontal_scale: int = 100) -> bytes:
    builder = PDFBuilder(version="1.7")
    glyph = builder.add(
        PDFStream(
            PDFDict(),
            b"500 0 d0\n1 0 0 rg\n0 0 500 700 re f\n",
        )
    )
    # Character-space +X maps to text-space +Y. The glyph rectangle itself is
    # rotated into the quadrant left/up from its text origin.
    font = builder.add(
        PDFDict(
            {
                "Type": PDFName("Font"),
                "Subtype": PDFName("Type3"),
                "FontBBox": [0, 0, 500, 700],
                "FontMatrix": [0, 0.001, -0.001, 0, 0, 0],
                "CharProcs": PDFDict({"A": glyph}),
                "Encoding": PDFDict({"Differences": [65, PDFName("A")]}),
                "FirstChar": 65,
                "LastChar": 65,
                "Widths": [500],
                "Resources": PDFDict(),
            }
        )
    )
    content = builder.add(
        PDFStream(
            PDFDict(),
            (
                f"BT /F3 20 Tf {horizontal_scale} Tz "
                "1 0 0 1 50 20 Tm (AA) Tj ET\n"
            ).encode("ascii"),
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 80],
                "Resources": PDFDict({"Font": PDFDict({"F3": font})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y_from_bottom: int):
    return page.surface.get_pixel(x, page.height - 1 - y_from_bottom)


def test_rotated_fontmatrix_advances_type3_glyphs_vertically():
    page = render_page_full(_pdf(), dpi=72)

    first = _pixel(page, 45, 25)
    second = _pixel(page, 45, 35)
    gap_after = _pixel(page, 45, 45)

    assert first.r > 0.9 and first.g < 0.1 and first.b < 0.1
    assert second.r > 0.9 and second.g < 0.1 and second.b < 0.1
    assert gap_after.r > 0.99 and gap_after.g > 0.99 and gap_after.b > 0.99


def test_horizontal_text_scale_does_not_scale_vertical_type3_advance():
    normal = render_page_full(_pdf(horizontal_scale=100), dpi=72)
    half = render_page_full(_pdf(horizontal_scale=50), dpi=72)

    # Both second glyphs start at the same Y because the FontMatrix turned the
    # width vector into text-space Y. Tz only scales text-space X.
    assert _pixel(normal, 45, 35).r > 0.9
    # Horizontal scaling shrinks the rotated glyph's text-space X dimension;
    # choose a point near the text origin that remains inside both.
    assert _pixel(half, 47, 35).r > 0.9
