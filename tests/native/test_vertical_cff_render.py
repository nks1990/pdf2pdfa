from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_cff_pdf_render import _cid_cff, _cid_font


def _vertical_font(*, dw2=None, w2=None) -> PDFDict:
    font = _cid_font(_cid_cff())
    font["Encoding"] = PDFName("Identity-V")
    descendant = font["DescendantFonts"][0]
    assert isinstance(descendant, PDFDict)
    if dw2 is not None:
        descendant["DW2"] = dw2
    if w2 is not None:
        descendant["W2"] = w2
    return font


def _pdf(font: PDFDict, content: bytes, *, extgstate: PDFDict | None = None) -> bytes:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(font)
    resources = PDFDict({"Font": PDFDict({"F": font_ref})})
    if extgstate is not None:
        resources["ExtGState"] = extgstate
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 120, 120],
                "Resources": resources,
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


def test_identity_v_default_metrics_stack_cff_cids_on_y_axis():
    font = _vertical_font()
    source = _pdf(
        font,
        b"BT /F 60 Tf 1 0 0 1 50 110 Tm <00640064> Tj ET\n",
    )
    page = render_page_full(source, dpi=72)

    # CID 100 is a rectangular glyph. DW2 defaults position it around x=50
    # and advance the second copy by -60 user units in Y.
    assert _bottom_pixel(page, 45, 80).r < 0.1
    assert _bottom_pixel(page, 45, 20).r < 0.1
    assert _bottom_pixel(page, 95, 80).r > 0.9


def test_w2_override_changes_vertical_origin_and_advance():
    font = _vertical_font(w2=[100, [-500, 100, 500]])
    source = _pdf(
        font,
        b"BT /F 40 Tf 1 0 0 1 30 90 Tm <00640064> Tj ET\n",
    )
    page = render_page_full(source, dpi=72)

    # vx=100 moves the horizontal-origin outline only four points left at
    # 40pt, while wy=-500 places the second glyph 20 points below the first.
    assert _bottom_pixel(page, 35, 78).r < 0.1
    assert _bottom_pixel(page, 35, 58).r < 0.1


def test_vertical_tj_adjustment_changes_y_not_x_in_real_pdf():
    font = _vertical_font(w2=[100, [-500, 100, 500]])
    plain = _pdf(
        font,
        b"BT /F 40 Tf 1 0 0 1 30 90 Tm [<0064> 250 <0064>] TJ ET\n",
    )
    page = render_page_full(plain, dpi=72)

    # wy=-500 is -20 points and TJ 250 contributes another -10 points.
    # Both copies remain on the same x column.
    assert _bottom_pixel(page, 35, 78).r < 0.1
    assert _bottom_pixel(page, 35, 48).r < 0.1
    assert _bottom_pixel(page, 75, 48).r > 0.9


def test_custom_dw2_supplies_default_vertical_metrics_when_w2_missing():
    font = _vertical_font(dw2=[500, -500])
    source = _pdf(
        font,
        b"BT /F 40 Tf 1 0 0 1 30 90 Tm <00640064> Tj ET\n",
    )
    page = render_page_full(source, dpi=72)
    assert _bottom_pixel(page, 25, 78).r < 0.1
    assert _bottom_pixel(page, 25, 58).r < 0.1
