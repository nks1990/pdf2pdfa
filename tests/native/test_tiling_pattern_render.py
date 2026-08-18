from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.pattern_render import PatternRenderError, UnsupportedPatternError


def _colored_cell(
    *,
    x_step: float = 10,
    y_step: float = 10,
    bbox=(0, 0, 10, 10),
    matrix=None,
    content: bytes | None = None,
    resources: PDFDict | None = None,
    paint_type: int = 1,
    tiling_type: int = 1,
) -> PDFStream:
    if content is None:
        content = (
            b"1 0 0 rg 0 0 5 10 re f\n"
            b"0 0 1 rg 5 0 5 10 re f\n"
        )
    dictionary = PDFDict(
        {
            "Type": PDFName("Pattern"),
            "PatternType": 1,
            "PaintType": paint_type,
            "TilingType": tiling_type,
            "BBox": list(bbox),
            "XStep": x_step,
            "YStep": y_step,
            "Resources": resources or PDFDict(),
        }
    )
    if matrix is not None:
        dictionary["Matrix"] = list(matrix)
    return PDFStream(dictionary, content)


def _pdf(
    pattern: PDFStream,
    *,
    content: bytes | None = None,
    width: int = 40,
    height: int = 20,
) -> bytes:
    builder = PDFBuilder(version="1.7")
    pattern_ref = builder.add(pattern)
    if content is None:
        content = f"/Pattern cs /P scn 0 0 {width} {height} re f\n".encode("ascii")
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, width, height],
                "Resources": PDFDict({"Pattern": PDFDict({"P": pattern_ref})}),
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


def _red(pixel) -> bool:
    return pixel.r > 0.95 and pixel.g < 0.05 and pixel.b < 0.05


def _blue(pixel) -> bool:
    return pixel.b > 0.95 and pixel.r < 0.05 and pixel.g < 0.05


def _white(pixel) -> bool:
    return pixel.r > 0.99 and pixel.g > 0.99 and pixel.b > 0.99


def test_colored_tiling_pattern_repeats_cell_on_lattice():
    page = render_page_full(_pdf(_colored_cell()), dpi=72)
    for x in (2, 12, 22, 32):
        assert _red(_pixel(page, x, 5))
    for x in (7, 17, 27, 37):
        assert _blue(_pixel(page, x, 5))
    assert _red(_pixel(page, 2, 15))
    assert _blue(_pixel(page, 17, 15))


def test_negative_xstep_generates_the_same_infinite_lattice():
    positive = render_page_full(_pdf(_colored_cell(x_step=10)), dpi=72)
    negative = render_page_full(_pdf(_colored_cell(x_step=-10)), dpi=72)
    assert positive.rgb_bytes() == negative.rgb_bytes()


def test_cell_bbox_clips_content_and_preserves_step_gaps():
    pattern = _colored_cell(
        bbox=(0, 0, 10, 10),
        x_step=15,
        content=b"1 0 0 rg -5 0 20 10 re f\n",
    )
    page = render_page_full(_pdf(pattern, width=45, height=10), dpi=72)
    assert _red(_pixel(page, 5, 5))
    assert _white(_pixel(page, 12, 5))
    assert _red(_pixel(page, 20, 5))
    assert _white(_pixel(page, 27, 5))


def test_pattern_matrix_scales_cell_geometry_and_lattice():
    pattern = _colored_cell(matrix=(2, 0, 0, 1, 0, 0))
    page = render_page_full(_pdf(pattern, width=40, height=10), dpi=72)
    assert _red(_pixel(page, 5, 5))
    assert _red(_pixel(page, 9, 5))
    assert _blue(_pixel(page, 15, 5))
    assert _red(_pixel(page, 25, 5))
    assert _blue(_pixel(page, 35, 5))


def test_parent_fill_path_clips_pattern_without_enumerating_entire_page():
    page = render_page_full(
        _pdf(
            _colored_cell(),
            content=b"/Pattern cs /P scn 5 0 20 10 re f\n",
            width=40,
            height=20,
        ),
        dpi=72,
    )
    assert _white(_pixel(page, 2, 5))
    assert not _white(_pixel(page, 7, 5))
    assert not _white(_pixel(page, 22, 5))
    assert _white(_pixel(page, 30, 5))
    assert _white(_pixel(page, 10, 15))


def test_tiling_cell_can_use_owned_shading_resource():
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [0, 0, 10, 0],
            "Extend": [True, True],
            "Function": PDFDict(
                {
                    "FunctionType": 2,
                    "Domain": [0, 1],
                    "C0": [1, 0, 0],
                    "C1": [0, 0, 1],
                    "N": 1,
                }
            ),
        }
    )
    pattern = _colored_cell(
        content=b"/Sh sh\n",
        resources=PDFDict({"Shading": PDFDict({"Sh": shading})}),
    )
    page = render_page_full(_pdf(pattern), dpi=72)
    assert _red(_pixel(page, 1, 5))
    assert _blue(_pixel(page, 9, 5))
    assert _red(_pixel(page, 11, 5))
    assert _blue(_pixel(page, 19, 5))


def test_cell_graphics_state_does_not_leak_to_parent_content():
    content = (
        b"/Pattern cs /P scn 0 0 20 10 re f\n"
        b"0 1 0 rg 20 0 20 10 re f\n"
    )
    page = render_page_full(_pdf(_colored_cell(), content=content, height=10), dpi=72)
    green = _pixel(page, 30, 5)
    assert green.g > 0.95 and green.r < 0.05 and green.b < 0.05


def test_painttype2_uncolored_pattern_remains_fail_closed():
    with pytest.raises(UnsupportedPatternError, match="PaintType 2"):
        render_page_full(_pdf(_colored_cell(paint_type=2)), dpi=72)


def test_zero_step_is_rejected_before_tile_enumeration():
    with pytest.raises(PatternRenderError, match="XStep/YStep"):
        render_page_full(_pdf(_colored_cell(x_step=0)), dpi=72)


def test_invalid_tiling_type_is_rejected():
    with pytest.raises(PatternRenderError, match="TilingType 9"):
        render_page_full(_pdf(_colored_cell(tiling_type=9)), dpi=72)


def test_tile_count_limit_is_fail_closed():
    tiny = _colored_cell(
        x_step=0.1,
        y_step=0.1,
        bbox=(0, 0, 0.1, 0.1),
        content=b"1 0 0 rg 0 0 .1 .1 re f\n",
    )
    with pytest.raises(UnsupportedPatternError, match="owned limit"):
        render_page_full(_pdf(tiny, width=100, height=100), dpi=72)


def test_unbalanced_cell_graphics_state_is_rejected_before_painting():
    bad = _colored_cell(content=b"q 1 0 0 rg 0 0 10 10 re f\n")
    with pytest.raises(PatternRenderError, match="unbalanced q/Q"):
        render_page_full(_pdf(bad), dpi=72)
