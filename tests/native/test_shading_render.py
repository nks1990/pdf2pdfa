from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError


def _function(c0, c1) -> PDFDict:
    return PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "C0": list(c0),
            "C1": list(c1),
            "N": 1,
        }
    )


def _pdf(
    shading: PDFDict,
    *,
    content: bytes = b"/Sh sh\n",
    extra_resources: PDFDict | None = None,
    width: int = 100,
    height: int = 20,
) -> bytes:
    builder = PDFBuilder(version="1.7")
    resources = PDFDict({"Shading": PDFDict({"Sh": shading})})
    if extra_resources:
        resources.update(extra_resources)
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


def _axial(*, coords=(0, 0, 100, 0), extend=None, bbox=None) -> PDFDict:
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": list(coords),
            "Function": _function((1, 0, 0), (0, 0, 1)),
        }
    )
    if extend is not None:
        shading["Extend"] = list(extend)
    if bbox is not None:
        shading["BBox"] = list(bbox)
    return shading


def _bottom_pixel(page, x: int, y: int = 10):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_axial_shading_interpolates_function_and_colorspace():
    page = render_page_full(_pdf(_axial()), dpi=72)
    left = _bottom_pixel(page, 5)
    middle = _bottom_pixel(page, 50)
    right = _bottom_pixel(page, 95)
    assert left.r > 0.9 and left.b < 0.1
    assert 0.4 < middle.r < 0.6 and 0.4 < middle.b < 0.6
    assert right.b > 0.9 and right.r < 0.1


def test_axial_extend_false_leaves_outside_axis_unpainted():
    page = render_page_full(_pdf(_axial(coords=(20, 0, 80, 0))), dpi=72)
    left = _bottom_pixel(page, 5)
    inside = _bottom_pixel(page, 25)
    right = _bottom_pixel(page, 95)
    assert left.r > 0.99 and left.g > 0.99 and left.b > 0.99
    assert inside.r > inside.b
    assert right.r > 0.99 and right.g > 0.99 and right.b > 0.99


def test_axial_extend_true_and_bbox_are_both_respected():
    page = render_page_full(
        _pdf(
            _axial(
                coords=(30, 0, 70, 0),
                extend=(True, True),
                bbox=(10, 0, 90, 20),
            )
        ),
        dpi=72,
    )
    assert _bottom_pixel(page, 5).r > 0.99
    assert _bottom_pixel(page, 15).r > 0.9
    assert _bottom_pixel(page, 85).b > 0.9
    assert _bottom_pixel(page, 95).r > 0.99


def test_shading_respects_current_clip_path():
    content = b"0 0 50 20 re W n /Sh sh\n"
    page = render_page_full(_pdf(_axial(), content=content), dpi=72)
    assert _bottom_pixel(page, 25).r < 0.9
    outside = _bottom_pixel(page, 75)
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_shading_uses_current_ctm():
    page = render_page_full(
        _pdf(_axial(coords=(0, 0, 50, 0)), content=b"2 0 0 1 0 0 cm /Sh sh\n"),
        dpi=72,
    )
    assert _bottom_pixel(page, 5).r > 0.9
    assert _bottom_pixel(page, 95).b > 0.9


def test_shading_honors_fill_alpha_from_extgstate():
    gs = PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})
    resources = PDFDict({"ExtGState": PDFDict({"GS": gs})})
    page = render_page_full(
        _pdf(
            _axial(coords=(0, 0, 100, 0), extend=(True, True)),
            content=b"/GS gs /Sh sh\n",
            extra_resources=resources,
        ),
        dpi=72,
    )
    left = _bottom_pixel(page, 1)
    assert left.r > 0.98
    assert 0.45 < left.g < 0.6
    assert 0.45 < left.b < 0.6


def test_radial_concentric_shading_interpolates_radius():
    shading = PDFDict(
        {
            "ShadingType": 3,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [50, 50, 0, 50, 50, 40],
            "Function": _function((1, 0, 0), (0, 0, 1)),
            "Extend": [False, False],
        }
    )
    page = render_page_full(_pdf(shading, width=100, height=100), dpi=72)
    center = _bottom_pixel(page, 50, 50)
    ring = _bottom_pixel(page, 80, 50)
    outside = _bottom_pixel(page, 98, 50)
    assert center.r > 0.9 and center.b < 0.1
    assert ring.b > ring.r
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_function_array_produces_one_component_per_function():
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [0, 0, 100, 0],
            "Function": [
                _function((1,), (0,)),
                _function((0,), (1,)),
                _function((0,), (0,)),
            ],
        }
    )
    page = render_page_full(_pdf(shading), dpi=72)
    left = _bottom_pixel(page, 5)
    right = _bottom_pixel(page, 95)
    assert left.r > left.g
    assert right.g > right.r


def test_mesh_shading_must_be_a_stream():
    shading = PDFDict(
        {
            "ShadingType": 4,
            "ColorSpace": PDFName("DeviceRGB"),
            "BitsPerCoordinate": 16,
            "BitsPerComponent": 8,
            "BitsPerFlag": 8,
            "Decode": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
        }
    )
    with pytest.raises(RenderingError, match="mesh shading shall be a stream"):
        render_page_full(_pdf(shading), dpi=72)
