from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.page_render import RenderingError
from pdf2pdfa.native.pattern_stroke import PatternStrokeRendererMixin

from tests.native.test_pattern_shading_render import _pattern as _shading_pattern
from tests.native.test_tiling_pattern_render import _colored_cell
from tests.native.test_uncolored_pattern_render import _pattern as _uncolored_cell


def _pdf(
    pattern: PDFDict | PDFStream,
    content: bytes,
    *,
    width: int = 100,
    height: int = 40,
    base_space: PDFName | None = None,
    extgstate: PDFDict | None = None,
) -> bytes:
    builder = PDFBuilder(version="1.7")
    pattern_ref = builder.add(pattern)
    resources = PDFDict({"Pattern": PDFDict({"P": pattern_ref})})
    if base_space is not None:
        resources["ColorSpace"] = PDFDict(
            {"UC": [PDFName("Pattern"), base_space]}
        )
    if extgstate is not None:
        resources["ExtGState"] = PDFDict({"GS": extgstate})
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, width, height],
                "Resources": resources,
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def _white(pixel) -> bool:
    return pixel.r > 0.98 and pixel.g > 0.98 and pixel.b > 0.98


def test_full_renderer_mro_includes_pattern_stroke_layer():
    assert PatternStrokeRendererMixin in FullOwnedPageRenderer.__mro__


def test_shading_pattern_stroke_reuses_gradient_painter_inside_stroke_mask():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"8 w /Pattern CS /P SCN 10 20 m 90 20 l S\n",
        ),
        dpi=72,
    )
    left = _pixel(page, 15, 20)
    right = _pixel(page, 85, 20)
    outside = _pixel(page, 50, 5)
    assert left.r > left.b
    assert right.b > right.r
    assert _white(outside)


def test_colored_tiling_pattern_strokes_repeating_cell():
    page = render_page_full(
        _pdf(
            _colored_cell(),
            b"10 w /Pattern CS /P SCN 5 20 m 95 20 l S\n",
        ),
        dpi=72,
    )
    assert _pixel(page, 12, 20).r > 0.9
    assert _pixel(page, 17, 20).b > 0.9
    assert _pixel(page, 22, 20).r > 0.9
    assert _white(_pixel(page, 50, 5))


def test_uncolored_tiling_pattern_stroke_uses_stroking_base_color():
    page = render_page_full(
        _pdf(
            _uncolored_cell(),
            b"10 w /UC CS 0 0 1 /P SCN 5 20 m 95 20 l S\n",
            base_space=PDFName("DeviceRGB"),
        ),
        dpi=72,
    )
    painted = _pixel(page, 12, 20)
    gap = _pixel(page, 17, 20)
    assert painted.b > 0.95 and painted.r < 0.1 and painted.g < 0.1
    assert _white(gap)


def test_stroke_alpha_CA_is_used_instead_of_nonstroking_ca():
    gs = PDFDict(
        {
            "Type": PDFName("ExtGState"),
            "CA": 0.5,
            "ca": 1.0,
        }
    )
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"/GS gs 8 w /Pattern CS /P SCN 10 20 m 90 20 l S\n",
            extgstate=gs,
        ),
        dpi=72,
    )
    painted = _pixel(page, 15, 20)
    # Red-ish source over white at CA=.5: green/blue stay around one half.
    assert painted.r > 0.9
    assert 0.4 < painted.g < 0.65
    assert 0.4 < painted.b < 0.8


def test_pattern_stroke_dash_uses_same_affine_dash_geometry_as_solid_stroke():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"8 w [10 10] 0 d /Pattern CS /P SCN 5 20 m 95 20 l S\n",
        ),
        dpi=72,
    )
    assert not _white(_pixel(page, 10, 20))
    assert _white(_pixel(page, 20, 20))
    assert not _white(_pixel(page, 30, 20))


def test_pattern_stroke_handles_nonuniform_ctm_without_scalar_width_substitution():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"q 2 0 0 1 0 0 cm 6 w /Pattern CS /P SCN 5 20 m 45 20 l S Q\n",
        ),
        dpi=72,
    )
    assert not _white(_pixel(page, 20, 20))
    assert not _white(_pixel(page, 80, 20))
    assert _white(_pixel(page, 50, 5))


def test_compound_B_preserves_solid_fill_and_pattern_stroke_independently():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"1 0 0 rg 8 w /Pattern CS /P SCN 10 10 80 20 re B\n",
        ),
        dpi=72,
    )
    center = _pixel(page, 50, 20)
    left_edge = _pixel(page, 10, 20)
    right_edge = _pixel(page, 90, 20)
    assert center.r > 0.95 and center.g < 0.05 and center.b < 0.05
    assert left_edge.r > left_edge.b
    assert right_edge.b > right_edge.r


def test_W_then_same_S_clips_outside_half_of_pattern_stroke():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"8 w /Pattern CS /P SCN 10 10 80 20 re W S\n",
        ),
        dpi=72,
    )
    # Stroke is centered on the rectangle boundary, but W applies the rectangle
    # as clip before S. The outward half must stay white.
    assert _white(_pixel(page, 7, 20))
    assert not _white(_pixel(page, 12, 20))
    assert _white(_pixel(page, 50, 7))
    assert not _white(_pixel(page, 50, 12))


def test_q_Q_restores_stroking_pattern_selection_and_color_space():
    page = render_page_full(
        _pdf(
            _shading_pattern(),
            b"q 8 w /Pattern CS /P SCN 5 10 m 45 10 l S Q\n"
            b"0 1 0 RG 8 w 55 30 m 95 30 l S\n",
        ),
        dpi=72,
    )
    patterned = _pixel(page, 15, 10)
    green = _pixel(page, 75, 30)
    assert not _white(patterned)
    assert green.g > 0.95 and green.r < 0.05 and green.b < 0.05


def test_pattern_stroke_requires_SCN_selection():
    with pytest.raises(RenderingError, match="missing SCN"):
        render_page_full(
            _pdf(
                _shading_pattern(),
                b"8 w /Pattern CS 10 20 m 90 20 l S\n",
            ),
            dpi=72,
        )
