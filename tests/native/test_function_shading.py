from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError


def _function2d() -> PDFStream:
    # Calculator starts with x y on the stack and pushes blue=0.5, producing
    # RGB = [x, y, 0.5].
    return PDFStream(
        PDFDict(
            {
                "FunctionType": 4,
                "Domain": [0, 1, 0, 1],
                "Range": [0, 1, 0, 1, 0, 1],
            }
        ),
        b"0.5",
    )


def _shading(*, matrix=(100, 0, 0, 100, 0, 0), bbox=None, background=None) -> PDFDict:
    value = PDFDict(
        {
            "ShadingType": 1,
            "ColorSpace": PDFName("DeviceRGB"),
            "Domain": [0, 1, 0, 1],
            "Matrix": list(matrix),
            "Function": _function2d(),
        }
    )
    if bbox is not None:
        value["BBox"] = list(bbox)
    if background is not None:
        value["Background"] = list(background)
    return value


def _pdf(shading: PDFDict, *, pattern: bool = False, content: bytes | None = None, alpha=None) -> bytes:
    builder = PDFBuilder(version="1.7")
    shading_ref = builder.add(shading)
    resources = PDFDict()
    if pattern:
        pattern_ref = builder.add(
            PDFDict(
                {
                    "Type": PDFName("Pattern"),
                    "PatternType": 2,
                    "Shading": shading_ref,
                }
            )
        )
        resources["Pattern"] = PDFDict({"P": pattern_ref})
        program = b"/Pattern cs /P scn 0 0 100 100 re f\n"
    else:
        resources["Shading"] = PDFDict({"Sh": shading_ref})
        program = b"/Sh sh\n"
    if content is not None:
        program = content
    if alpha is not None:
        resources["ExtGState"] = PDFDict({"GS": PDFDict({"ca": alpha, "CA": alpha})})
        program = b"/GS gs " + program
    contents = builder.add(PDFStream(PDFDict(), program))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
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


def test_function_shading_maps_two_dimensional_domain_through_matrix():
    page = render_page_full(_pdf(_shading()), dpi=72)
    low = _bottom_pixel(page, 10, 10)
    high = _bottom_pixel(page, 90, 90)
    assert 0.05 < low.r < 0.15 and 0.05 < low.g < 0.15
    assert 0.45 < low.b < 0.55
    assert 0.85 < high.r < 0.95 and 0.85 < high.g < 0.95
    assert 0.45 < high.b < 0.55


def test_function_shading_bbox_is_evaluated_in_target_space():
    page = render_page_full(_pdf(_shading(bbox=(25, 25, 75, 75))), dpi=72)
    inside = _bottom_pixel(page, 50, 50)
    outside = _bottom_pixel(page, 10, 10)
    assert min(inside.r, inside.g, inside.b) < 0.9
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_function_shading_background_fills_target_outside_domain():
    page = render_page_full(
        _pdf(_shading(matrix=(50, 0, 0, 50, 0, 0), background=(1, 0, 0))),
        dpi=72,
    )
    inside = _bottom_pixel(page, 25, 25)
    outside = _bottom_pixel(page, 75, 75)
    assert 0.4 < inside.r < 0.6 and 0.4 < inside.g < 0.6
    assert outside.r > 0.98 and outside.g < 0.02 and outside.b < 0.02


def test_function_shading_respects_current_ctm_and_shading_matrix():
    page = render_page_full(
        _pdf(
            _shading(matrix=(50, 0, 0, 50, 0, 0)),
            content=b"2 0 0 2 0 0 cm /Sh sh\n",
        ),
        dpi=72,
    )
    high = _bottom_pixel(page, 90, 90)
    assert 0.85 < high.r < 0.95 and 0.85 < high.g < 0.95


def test_pattern_type2_function_shading_uses_same_dispatcher():
    direct = render_page_full(_pdf(_shading()), dpi=72)
    patterned = render_page_full(_pdf(_shading(), pattern=True), dpi=72)
    assert direct.rgb_bytes() == patterned.rgb_bytes()


def test_function_shading_honors_fill_alpha():
    page = render_page_full(_pdf(_shading(), alpha=0.5), dpi=72)
    low = _bottom_pixel(page, 10, 10)
    # Blend x/y≈0.1, blue=.5 over white at 50% alpha.
    assert 0.5 < low.r < 0.6 and 0.5 < low.g < 0.6
    assert 0.7 < low.b < 0.8


def test_function_shading_rejects_one_input_function():
    bad_function = PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "C0": [0, 0, 0],
            "C1": [1, 1, 1],
            "N": 1,
        }
    )
    shading = _shading()
    shading["Function"] = bad_function
    with pytest.raises(RenderingError, match="exactly two inputs"):
        render_page_full(_pdf(shading), dpi=72)
