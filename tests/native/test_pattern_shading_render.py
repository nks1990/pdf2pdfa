from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.pattern_render import UnsupportedPatternError


def _function(c0=(1, 0, 0), c1=(0, 0, 1)) -> PDFDict:
    return PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "C0": list(c0),
            "C1": list(c1),
            "N": 1,
        }
    )


def _shading(*, coords=(0, 0, 100, 0)) -> PDFDict:
    return PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": list(coords),
            "Extend": [True, True],
            "Function": _function(),
        }
    )


def _pattern(
    *,
    matrix=None,
    extgstate: PDFDict | None = None,
    pattern_type: int = 2,
) -> PDFDict:
    value = PDFDict({"Type": PDFName("Pattern"), "PatternType": pattern_type})
    if pattern_type == 2:
        value["Shading"] = _shading(coords=(0, 0, 50 if matrix else 100, 0))
    if matrix is not None:
        value["Matrix"] = list(matrix)
    if extgstate is not None:
        value["ExtGState"] = extgstate
    return value


def _pdf(
    content: bytes,
    *,
    pattern: PDFDict | PDFStream | None = None,
    width: int = 100,
    height: int = 40,
) -> bytes:
    builder = PDFBuilder(version="1.7")
    resources = PDFDict()
    if pattern is not None:
        pattern_ref = builder.add(pattern)
        resources["Pattern"] = PDFDict({"P": pattern_ref})
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


def _pixel(page, x: int, y_from_bottom: int):
    return page.surface.get_pixel(x, page.height - 1 - y_from_bottom)


def test_shading_pattern_fills_path_and_clips_to_path_geometry():
    page = render_page_full(
        _pdf(
            b"/Pattern cs /P scn 10 10 80 20 re f\n",
            pattern=_pattern(),
        ),
        dpi=72,
    )
    outside = _pixel(page, 5, 20)
    left = _pixel(page, 15, 20)
    right = _pixel(page, 85, 20)
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99
    assert left.r > left.b
    assert right.b > right.r


def test_pattern_matrix_is_composed_before_page_ctm():
    # Pattern shading axis is 0..50. A Pattern Matrix with x2 scale makes it
    # cover the 0..100 user-space page.
    page = render_page_full(
        _pdf(
            b"/Pattern cs /P scn 0 0 100 40 re f\n",
            pattern=_pattern(matrix=(2, 0, 0, 1, 0, 0)),
        ),
        dpi=72,
    )
    assert _pixel(page, 5, 20).r > 0.9
    assert _pixel(page, 95, 20).b > 0.9


def test_pattern_fill_respects_evenodd_hole():
    content = (
        b"/Pattern cs /P scn "
        b"0 0 100 40 re 30 10 40 20 re f*\n"
    )
    page = render_page_full(_pdf(content, pattern=_pattern()), dpi=72)
    painted = _pixel(page, 10, 20)
    hole = _pixel(page, 50, 20)
    assert painted.r < 0.99 or painted.b < 0.99
    assert hole.r > 0.99 and hole.g > 0.99 and hole.b > 0.99


def test_q_q_restores_pattern_color_state():
    content = (
        b"q /Pattern cs /P scn 0 0 50 20 re f Q\n"
        b"1 0 0 rg 50 20 50 20 re f\n"
    )
    page = render_page_full(_pdf(content, pattern=_pattern()), dpi=72)
    gradient = _pixel(page, 25, 10)
    solid = _pixel(page, 75, 30)
    assert gradient.r > gradient.b
    assert solid.r > 0.98 and solid.g < 0.02 and solid.b < 0.02


def test_device_color_operator_exits_pattern_state():
    content = (
        b"/Pattern cs /P scn 0 0 50 20 re f\n"
        b"0 1 0 rg 50 0 50 20 re f\n"
    )
    page = render_page_full(_pdf(content, pattern=_pattern()), dpi=72)
    green = _pixel(page, 75, 10)
    assert green.g > 0.98 and green.r < 0.02 and green.b < 0.02


def test_pattern_extgstate_alpha_is_applied():
    page = render_page_full(
        _pdf(
            b"/Pattern cs /P scn 0 0 100 40 re f\n",
            pattern=_pattern(extgstate=PDFDict({"ca": 0.5})),
        ),
        dpi=72,
    )
    left = _pixel(page, 1, 20)
    assert left.r > 0.98
    assert 0.45 < left.g < 0.6
    assert 0.45 < left.b < 0.6


def test_combined_pattern_fill_and_solid_stroke_preserves_stroke():
    content = (
        b"0 0 0 RG 2 w /Pattern cs /P scn "
        b"10 10 80 20 re B\n"
    )
    page = render_page_full(_pdf(content, pattern=_pattern()), dpi=72)
    center = _pixel(page, 50, 20)
    edge = _pixel(page, 10, 20)
    assert center.r < 0.9 or center.b < 0.9
    assert edge.r < 0.2 and edge.g < 0.2 and edge.b < 0.2


def test_pattern_stroke_remains_fail_closed():
    with pytest.raises(UnsupportedPatternError, match="pattern-colored strokes"):
        render_page_full(
            _pdf(
                b"/Pattern CS /P SCN 10 10 80 20 re S\n",
                pattern=_pattern(),
            ),
            dpi=72,
        )


def test_tiling_pattern_remains_explicitly_separate():
    tiling = PDFStream(
        PDFDict(
            {
                "Type": PDFName("Pattern"),
                "PatternType": 1,
                "PaintType": 1,
                "TilingType": 1,
                "BBox": [0, 0, 10, 10],
                "XStep": 10,
                "YStep": 10,
                "Resources": PDFDict(),
            }
        ),
        b"0 0 10 10 re f\n",
    )
    with pytest.raises(UnsupportedPatternError, match="PatternType 2"):
        render_page_full(
            _pdf(b"/Pattern cs /P scn 0 0 100 40 re f\n", pattern=tiling),
            dpi=72,
        )


def test_uncolored_pattern_space_cannot_be_used_with_shading_pattern():
    builder = PDFBuilder(version="1.7")
    pattern_ref = builder.add(_pattern())
    resources = PDFDict(
        {
            "Pattern": PDFDict({"P": pattern_ref}),
            "ColorSpace": PDFDict(
                {"PCS": [PDFName("Pattern"), PDFName("DeviceRGB")]}
            ),
        }
    )
    contents = builder.add(
        PDFStream(
            PDFDict(),
            b"/PCS cs 1 0 0 /P scn 0 0 100 40 re f\n",
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 40],
                "Resources": resources,
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    with pytest.raises(UnsupportedPatternError, match="uncolored Pattern"):
        render_page_full(builder.to_bytes(), dpi=72)
