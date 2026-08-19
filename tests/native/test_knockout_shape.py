from __future__ import annotations

import pytest

from pdf2pdfa.native.knockout import (
    ShapeAccumulator,
    knockout_pixel,
    knockout_surface,
    union_coverage,
)
from pdf2pdfa.native.raster import Color, Surface


def _approx_color(actual: Color, expected: Color, tolerance: float = 1.0 / 255.0):
    assert actual.r == pytest.approx(expected.r, abs=tolerance)
    assert actual.g == pytest.approx(expected.g, abs=tolerance)
    assert actual.b == pytest.approx(expected.b, abs=tolerance)
    assert actual.a == pytest.approx(expected.a, abs=tolerance)


def test_half_opaque_source_with_full_shape_fully_knocks_out_backdrop():
    result = knockout_pixel(
        Color(0, 0, 1, 1),
        Color(1, 0, 0, 0.5),
        1.0,
    )
    _approx_color(result, Color(1, 0, 0, 0.5))


def test_half_shape_interpolates_color_and_alpha_independently_of_opacity():
    result = knockout_pixel(
        Color(0, 0, 1, 1),
        Color(1, 0, 0, 0.5),
        0.5,
    )
    _approx_color(result, Color(0.5, 0, 0.5, 0.75))


def test_zero_shape_is_identity_even_when_source_is_opaque():
    backdrop = Color(0.1, 0.2, 0.3, 0.4)
    _approx_color(knockout_pixel(backdrop, Color(1, 1, 1, 1), 0), backdrop)


def test_shape_accumulator_uses_coverage_union_not_addition():
    acc = ShapeAccumulator.empty(1, 1)
    acc.add(bytes([128]))
    acc.add(bytes([128]))
    assert acc.samples[0] == pytest.approx(round(0.75 * 255), abs=1)
    assert union_coverage(0.5, 0.5) == pytest.approx(0.75)


def test_shape_accumulator_scale_can_represent_alpha_is_shape():
    acc = ShapeAccumulator.empty(1, 1)
    acc.add(bytes([255]), scale=0.5)
    assert acc.samples[0] == pytest.approx(128, abs=1)


def test_knockout_surface_respects_destination_clip_without_changing_source_alpha():
    dst = Surface(1, 1, background=Color(0, 0, 1, 1))
    dst.clip[0] = 128
    src = Surface(1, 1, background=Color(1, 0, 0, 0.5))
    knockout_surface(dst, src, bytes([255]))
    # clip halves the shape: color is half-way red/blue and alpha is 0.75.
    _approx_color(dst.get_pixel(0, 0), Color(0.5, 0, 0.5, 0.75), tolerance=2 / 255)


def test_knockout_surface_rejects_mismatched_shape_plane():
    with pytest.raises(ValueError, match="shape dimensions"):
        knockout_surface(Surface(2, 2), Surface(2, 2), b"\xff")
