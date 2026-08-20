from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError

from tests.native.test_annotation_render import _appearance, _pdf


def test_annotation_nozoom_is_fail_closed_until_transform_semantics_are_owned():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    with pytest.raises(RenderingError, match="NoZoom"):
        render_page_full(_pdf(ap, flags=4 | 8), dpi=72)


def test_annotation_norotate_is_fail_closed_until_transform_semantics_are_owned():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    with pytest.raises(RenderingError, match="NoRotate"):
        render_page_full(_pdf(ap, flags=4 | 16), dpi=72)


def test_annotation_togglenoview_is_fail_closed_for_static_visual_fidelity():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    with pytest.raises(RenderingError, match="ToggleNoView"):
        render_page_full(_pdf(ap, flags=4 | 256), dpi=72)


def test_standard_annotation_rect_tracks_page_rotate_90():
    builder = PDFBuilder(version="1.7")
    ap_ref = builder.add(_appearance(b"1 0 0 rg 0 0 10 10 re f\n"))
    annot_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Annot"),
                "Subtype": PDFName("Stamp"),
                "Rect": [10, 10, 30, 30],
                "F": 4,
                "AP": PDFDict({"N": ap_ref}),
            }
        )
    )
    content = builder.add(PDFStream(PDFDict(), b""))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 50],
                "Rotate": 90,
                "Resources": PDFDict(),
                "Contents": content,
                "Annots": [annot_ref],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)

    page = render_page_full(builder.to_bytes(), dpi=72)
    assert (page.width, page.height) == (50, 100)
    red_inside = 0
    red_outside = 0
    for y in range(page.height):
        for x in range(page.width):
            pixel = page.surface.get_pixel(x, y)
            red = pixel.r > 0.95 and pixel.g < 0.05 and pixel.b < 0.05
            if 10 <= x < 30 and 10 <= y < 30:
                red_inside += int(red)
            else:
                red_outside += int(red)
    assert red_inside > 300
    assert red_outside == 0
