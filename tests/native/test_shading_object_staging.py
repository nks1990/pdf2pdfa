from __future__ import annotations

import pytest

from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_mesh_shading45 import _bottom_pixel, _pdf, _v4


def _background_triangle() -> PDFStream:
    # One blue triangle over a red shading Background.  Background and mesh are
    # components of ONE shading object: outer alpha must be applied once after
    # the intrinsic blue triangle has replaced the red background.
    data = b"".join(
        [
            _v4(0, 0, 0, 0, 0, 255),
            _v4(0, 255, 0, 0, 0, 255),
            _v4(0, 0, 255, 0, 0, 255),
        ]
    )
    return PDFStream(
        PDFDict(
            {
                "ShadingType": 4,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerCoordinate": 8,
                "BitsPerComponent": 8,
                "BitsPerFlag": 8,
                "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
                "BBox": [0, 0, 100, 100],
                "Background": [1, 0, 0],
            }
        ),
        data,
    )


def test_mesh_background_and_mesh_receive_outer_alpha_only_once():
    page = render_page_full(_pdf(_background_triangle(), alpha=0.5), dpi=72)

    inside = _bottom_pixel(page, 15, 15)
    outside = _bottom_pixel(page, 85, 85)

    # Blue intrinsic mesh over white at 50% outer alpha.
    assert inside.r == pytest.approx(0.5, abs=2 / 255)
    assert inside.g == pytest.approx(0.5, abs=2 / 255)
    assert inside.b > 0.99

    # Red intrinsic Background over white at the same single 50% alpha.
    assert outside.r > 0.99
    assert outside.g == pytest.approx(0.5, abs=2 / 255)
    assert outside.b == pytest.approx(0.5, abs=2 / 255)


def test_mesh_background_staging_remains_equivalent_inside_pattern_type2():
    direct = render_page_full(_pdf(_background_triangle(), alpha=0.5), dpi=72)
    patterned = render_page_full(
        _pdf(_background_triangle(), pattern=True, alpha=0.5),
        dpi=72,
    )
    assert direct.rgb_bytes() == patterned.rgb_bytes()
