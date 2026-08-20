from __future__ import annotations

import pytest

from pdf2pdfa.native.mesh_shading import UnsupportedMeshShadingError
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.patch_shading import (
    PatchMesh,
    PatchPoint,
    _patch_divisions,
)
from pdf2pdfa.native.raster import Matrix

from tests.native.test_mesh_shading45 import _bottom_pixel, _pdf
from tests.native.test_patch_shading67 import BOUNDARY12, _record


def _function_patch() -> PDFStream:
    # One encoded scalar t per corner. The owned Function maps t from red to
    # blue; patch interpolation therefore occurs in parameter space before the
    # Function is evaluated.
    function = PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "Range": [0, 1, 0, 1, 0, 1],
            "C0": [1, 0, 0],
            "C1": [0, 0, 1],
            "N": 1,
        }
    )
    data = bytearray([0])
    for x, y in BOUNDARY12:
        data.extend((x, y))
    data.extend((0, 255, 255, 0))
    return PDFStream(
        PDFDict(
            {
                "ShadingType": 6,
                "ColorSpace": PDFName("DeviceRGB"),
                "Function": function,
                "BitsPerCoordinate": 8,
                "BitsPerComponent": 8,
                "BitsPerFlag": 8,
                "Decode": [0, 100, 0, 100, 0, 1],
                "BBox": [0, 0, 100, 100],
            }
        ),
        bytes(data),
    )


def test_function_based_patch_interpolates_parameter_before_color_function():
    page = render_page_full(_pdf(_function_patch()), dpi=72)
    left = _bottom_pixel(page, 3, 50)
    right = _bottom_pixel(page, 96, 50)
    center = _bottom_pixel(page, 50, 50)
    assert left.r > left.b
    assert right.b > right.r
    assert center.r == pytest.approx(center.b, abs=0.12)
    assert center.g < 0.08


def test_patch_tessellation_fails_closed_when_device_curvature_exceeds_limit():
    zero = (0.0, 0.0, 0.0)
    poles = (
        (PatchPoint(0, 0), PatchPoint(0.33, 1000), PatchPoint(0.66, -1000), PatchPoint(1, 0)),
        (PatchPoint(0, 0.33), PatchPoint(0.33, 1000), PatchPoint(0.66, -1000), PatchPoint(1, 0.33)),
        (PatchPoint(0, 0.66), PatchPoint(0.33, -1000), PatchPoint(0.66, 1000), PatchPoint(1, 0.66)),
        (PatchPoint(0, 1), PatchPoint(0.33, -1000), PatchPoint(0.66, 1000), PatchPoint(1, 1)),
    )
    patch = PatchMesh(poles, (zero, zero, zero, zero))
    with pytest.raises(UnsupportedMeshShadingError, match="subdivisions beyond owned renderer limit"):
        _patch_divisions((patch,), Matrix(a=1000, d=1000))


def test_patch_triangle_budget_is_checked_before_rasterization():
    zero = (0.0, 0.0, 0.0)
    flat = (
        (PatchPoint(0, 0), PatchPoint(1 / 3, 0), PatchPoint(2 / 3, 0), PatchPoint(1, 0)),
        (PatchPoint(0, 1 / 3), PatchPoint(1 / 3, 1 / 3), PatchPoint(2 / 3, 1 / 3), PatchPoint(1, 1 / 3)),
        (PatchPoint(0, 2 / 3), PatchPoint(1 / 3, 2 / 3), PatchPoint(2 / 3, 2 / 3), PatchPoint(1, 2 / 3)),
        (PatchPoint(0, 1), PatchPoint(1 / 3, 1), PatchPoint(2 / 3, 1), PatchPoint(1, 1)),
    )
    patch = PatchMesh(flat, (zero, zero, zero, zero))
    # 25,000 flat patches at the baseline 8x8 subdivision produce 3.2M
    # triangles and are within the 4M budget.  One more cannot be created by
    # the stream decoder because MAX_PATCHES already bounds the patch count;
    # this assertion documents the interaction between both resource limits.
    divisions = _patch_divisions((patch,) * 25_000, Matrix())
    assert divisions == 8
