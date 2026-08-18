from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.render import render_page


def _pdf(content: bytes) -> bytes:
    builder = PDFBuilder(version="1.7")
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref,
        "MediaBox": [0,0,100,100], "Resources": PDFDict(), "Contents": contents,
    }))
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _dark(page, x: int, y: int) -> bool:
    pixel = page.surface.get_pixel(x, y)
    return pixel.r < 0.5 and pixel.g < 0.5 and pixel.b < 0.5


def test_dash_pattern_draws_on_off_segments():
    page = render_page(_pdf(b"[10 10] 0 d 2 w 10 50 m 70 50 l S\n"), dpi=72)
    assert _dark(page, 15, 50)
    assert not _dark(page, 25, 50)
    assert _dark(page, 35, 50)


def test_nonuniform_ctm_stroke_uses_user_space_width_not_scalar_average():
    # User-space horizontal line width 4 under x2/y1 CTM should remain 4 device
    # pixels thick vertically, while its length doubles horizontally.
    page = render_page(_pdf(b"q 2 0 0 1 0 0 cm 4 w 10 50 m 30 50 l S Q\n"), dpi=72)
    assert _dark(page, 30, 49)
    assert _dark(page, 30, 51)
    assert not _dark(page, 30, 46)


def test_square_cap_extends_half_line_width():
    page = render_page(_pdf(b"2 J 4 w 20 50 m 40 50 l S\n"), dpi=72)
    assert _dark(page, 18, 50)
    assert _dark(page, 41, 50)
    assert not _dark(page, 17, 50)


def test_clip_committed_after_same_path_painting():
    # First rectangle paints blue outside its own future clip. If W were applied
    # before f, painting would be indistinguishable for this same path, so use
    # combined path: clip only left half via W n, then red full-page fill.
    page = render_page(_pdf(
        b"0 0 1 rg 0 0 100 100 re f "
        b"0 0 50 100 re W n "
        b"1 0 0 rg 0 0 100 100 re f\n"
    ), dpi=72)
    left = page.surface.get_pixel(25, 50)
    right = page.surface.get_pixel(75, 50)
    assert left.r > 0.99 and left.b < 0.01
    assert right.b > 0.99 and right.r < 0.01


def test_w_with_paint_does_not_clip_that_paint_prematurely():
    # W f: according to PDF graphics state, fill is painted under the old clip,
    # and the new clip applies after path termination.
    page = render_page(_pdf(
        b"0 0 1 rg 0 0 100 100 re f "
        b"1 0 0 rg 0 0 50 100 re 50 0 50 100 re W f\n"
    ), dpi=72)
    assert page.surface.get_pixel(25, 50).r > 0.99
    assert page.surface.get_pixel(75, 50).r > 0.99
