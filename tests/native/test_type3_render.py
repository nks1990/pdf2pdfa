from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.render import render_page
from pdf2pdfa.native.page_render import UnsupportedRenderingError


def _type3_font(builder: PDFBuilder, *, nested_text: bool = False) -> PDFDict:
    a_content = (
        b"500 0 d0\n"
        + (b"BT ET\n" if nested_text else b"")
        + b"0 0 500 700 re f\n"
    )
    # B changes fill color internally. The synthetic glyph graphics-state frame
    # must keep that blue color from leaking into following page painting.
    b_content = b"500 0 0 0 500 700 d1\n0 0 1 rg\n0 0 500 700 re f\n"
    a = builder.add(PDFStream(PDFDict(), a_content))
    b = builder.add(PDFStream(PDFDict(), b_content))
    return PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type3"),
            "FontBBox": [0, 0, 500, 700],
            "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
            "CharProcs": PDFDict({"A": a, "B": b}),
            "Encoding": PDFDict(
                {
                    "Type": PDFName("Encoding"),
                    "Differences": [65, PDFName("A"), PDFName("B")],
                }
            ),
            "FirstChar": 65,
            "LastChar": 66,
            "Widths": [500, 500],
            "Resources": PDFDict(),
        }
    )


def _pdf(*, content: bytes, nested_text: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    font = builder.add(_type3_font(builder, nested_text=nested_text))
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F3": font})}),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y_from_bottom: int):
    return page.surface.get_pixel(x, page.height - 1 - y_from_bottom)


def test_type3_differences_font_renders_charprocs_and_advances():
    page = render_page(
        _pdf(
            content=(
                b"1 0 0 rg\n"
                b"BT /F3 20 Tf 1 0 0 1 10 20 Tm (AB) Tj ET\n"
                # If B's internal blue leaked, this rectangle would be blue.
                b"60 20 10 10 re f\n"
            )
        ),
        dpi=72,
    )
    a = _pixel(page, 14, 25)
    b = _pixel(page, 24, 25)
    after = _pixel(page, 65, 25)
    assert a.r > 0.9 and a.g < 0.1 and a.b < 0.1
    assert b.b > 0.9 and b.r < 0.1 and b.g < 0.1
    assert after.r > 0.9 and after.b < 0.1


def test_type3_invisible_text_advances_without_painting():
    page = render_page(
        _pdf(content=b"0 0 0 rg BT /F3 20 Tf 3 Tr 1 0 0 1 10 20 Tm (AB) Tj ET"),
        dpi=72,
    )
    for y in range(page.height):
        for x in range(page.width):
            pixel = page.surface.get_pixel(x, y)
            assert pixel.r > 0.99 and pixel.g > 0.99 and pixel.b > 0.99


def test_type3_charproc_must_start_with_d0_or_d1():
    builder = PDFBuilder(version="1.7")
    bad = builder.add(PDFStream(PDFDict(), b"0 0 500 700 re f\n"))
    font = builder.add(
        PDFDict(
            {
                "Type": PDFName("Font"),
                "Subtype": PDFName("Type3"),
                "FontBBox": [0, 0, 500, 700],
                "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
                "CharProcs": PDFDict({"A": bad}),
                "Encoding": PDFDict({"Differences": [65, PDFName("A")]}),
                "FirstChar": 65,
                "LastChar": 65,
                "Widths": [500],
                "Resources": PDFDict(),
            }
        )
    )
    contents = builder.add(PDFStream(PDFDict(), b"BT /F3 20 Tf 10 20 Td (A) Tj ET"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,100,100],
        "Resources": PDFDict({"Font": PDFDict({"F3": font})}), "Contents": contents,
    }))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)

    with pytest.raises(Exception, match="begin with d0 or d1"):
        render_page(builder.to_bytes(), dpi=72)


def test_type3_nested_text_is_fail_closed():
    with pytest.raises(UnsupportedRenderingError, match="nested text"):
        render_page(
            _pdf(
                content=b"BT /F3 20 Tf 1 0 0 1 10 20 Tm (A) Tj ET",
                nested_text=True,
            ),
            dpi=72,
        )


def test_type3_stroke_render_mode_remains_explicitly_unsupported():
    with pytest.raises(Exception, match="Type3 text rendering modes"):
        render_page(
            _pdf(content=b"BT /F3 20 Tf 1 Tr 1 0 0 1 10 20 Tm (A) Tj ET"),
            dpi=72,
        )
