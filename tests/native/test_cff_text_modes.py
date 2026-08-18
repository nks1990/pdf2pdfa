from __future__ import annotations

from pdf2pdfa.native.objects import PDFName
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_cff_pdf_render import (
    _bottom_pixel,
    _pdf,
    _simple_cff,
    _simple_font,
)


def _font():
    return _simple_font(_simple_cff(b"A"), encoding=PDFName("WinAnsiEncoding"))


def test_cff_stroke_text_mode_uses_owned_raster_width_api():
    page = render_page_full(_pdf(_font(), b"A", render_mode=1), dpi=72)
    # Interior remains white in stroke-only mode, while an outline pixel paints.
    interior = _bottom_pixel(page, 25, 35)
    edge = _bottom_pixel(page, 16, 35)
    assert interior.r > 0.95 and interior.g > 0.95 and interior.b > 0.95
    assert min(edge.r, edge.g, edge.b) < 0.8


def test_cff_fill_and_stroke_mode_paints_interior_and_outline():
    page = render_page_full(_pdf(_font(), b"A", render_mode=2), dpi=72)
    interior = _bottom_pixel(page, 25, 35)
    edge = _bottom_pixel(page, 16, 35)
    assert min(interior.r, interior.g, interior.b) < 0.1
    assert min(edge.r, edge.g, edge.b) < 0.8


def test_cff_invisible_mode_does_not_paint():
    page = render_page_full(_pdf(_font(), b"A", render_mode=3), dpi=72)
    assert all(value == 255 for value in page.rgb_bytes())
