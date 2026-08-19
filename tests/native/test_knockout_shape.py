from __future__ import annotations

import pytest

from pdf2pdfa.native.knockout import (
    KnockoutSurface,
    ShapeAccumulator,
    composite_knockout_element,
    knockout_surface,
    union_coverage,
)
from pdf2pdfa.native.raster import Color, Surface


def _approx_color(actual: Color, expected: Color, tolerance: float = 2.0 / 255.0):
    assert actual.r == pytest.approx(expected.r, abs=tolerance)
    assert actual.g == pytest.approx(expected.g, abs=tolerance)
    assert actual.b == pytest.approx(expected.b, abs=tolerance)
    assert actual.a == pytest.approx(expected.a, abs=tolerance)


def test_isolated_full_shape_keeps_source_color_and_source_opacity():
    transparent = Color(0, 0, 0, 0)
    result, group_alpha = composite_knockout_element(
        transparent,
        transparent,
        0.0,
        Color(1, 0, 0, 0.5),
        1.0,
    )
    _approx_color(result, Color(1, 0, 0, 0.5))
    assert group_alpha == pytest.approx(0.5)


def test_nonisolated_full_shape_composites_opacity_against_fixed_backdrop():
    blue = Color(0, 0, 1, 1)
    result, group_alpha = composite_knockout_element(
        blue,
        blue,
        0.0,
        Color(1, 0, 0, 0.5),
        1.0,
    )
    _approx_color(result, Color(0.5, 0, 0.5, 1))
    # With an opaque non-isolated backdrop, group alpha carries shape even when
    # source opacity is fractional; it cannot be inferred from visible alpha=1.
    assert group_alpha == pytest.approx(1.0)


def test_fractional_shape_is_applied_once_not_squared_into_opacity():
    transparent = Color(0, 0, 0, 0)
    result, group_alpha = composite_knockout_element(
        transparent,
        transparent,
        0.0,
        Color(1, 0, 0, 0.25),
        0.5,
    )
    # source alpha = shape * opacity = 0.5 * 0.25 = 0.125. The straight
    # source color remains red rather than being multiplied by shape again.
    _approx_color(result, Color(1, 0, 0, 0.125))
    assert group_alpha == pytest.approx(0.125)


def test_second_full_shape_knockout_removes_first_sibling_color():
    blue = Color(0, 0, 1, 1)
    first, alpha_g = composite_knockout_element(
        blue, blue, 0.0, Color(1, 0, 0, 0.5), 1.0
    )
    second, alpha_g = composite_knockout_element(
        blue, first, alpha_g, Color(0, 1, 0, 0.5), 1.0
    )
    _approx_color(second, Color(0, 0.5, 0.5, 1))
    assert alpha_g == pytest.approx(1.0)


def test_half_shape_over_opaque_backdrop_has_quarter_source_contribution():
    blue = Color(0, 0, 1, 1)
    result, _ = composite_knockout_element(
        blue,
        blue,
        0.0,
        Color(1, 0, 0, 0.5),
        0.5,
    )
    _approx_color(result, Color(0.25, 0, 0.75, 1))


def test_shape_accumulator_uses_coverage_union_not_addition():
    acc = ShapeAccumulator.empty(1, 1)
    acc.add(bytes([128]))
    acc.add(bytes([128]))
    assert acc.samples[0] == pytest.approx(round(0.75 * 255), abs=1)
    assert union_coverage(0.5, 0.5) == pytest.approx(0.75)


def test_knockout_surface_respects_destination_clip_as_shape():
    dst = Surface(1, 1, background=Color(0, 0, 1, 1))
    dst.clip[0] = 128
    src = Surface(1, 1, background=Color(1, 0, 0, 0.5))
    knockout_surface(dst, src, bytes([255]))
    _approx_color(dst.get_pixel(0, 0), Color(0.25, 0, 0.75, 1))


def test_knockout_surface_rejects_mismatched_shape_plane():
    with pytest.raises(ValueError, match="shape dimensions"):
        knockout_surface(Surface(2, 2), Surface(2, 2), b"\xff")


def test_knockout_surface_tracks_fixed_backdrop_and_group_alpha_per_pixel():
    backdrop = Surface(1, 1, background=Color(0, 0, 1, 1))
    surface = KnockoutSurface(backdrop)

    surface.composite_pixel(0, 0, Color(1, 0, 0, 0.5))
    _approx_color(surface.get_pixel(0, 0), Color(0.5, 0, 0.5, 1))
    assert surface.group_alpha[0] == 255

    surface.composite_pixel(0, 0, Color(0, 1, 0, 0.5))
    _approx_color(surface.get_pixel(0, 0), Color(0, 0.5, 0.5, 1))
    assert surface.shape.samples[0] == 255


def test_knockout_surface_antialias_shape_is_independent_of_object_opacity():
    backdrop = Surface(1, 1, background=Color(0, 0, 0, 0))
    surface = KnockoutSurface(backdrop)
    surface.composite_pixel(
        0,
        0,
        Color(1, 0, 0, 0.25),
        coverage=0.5,
    )
    assert surface.shape.samples[0] == pytest.approx(128, abs=1)
    assert surface.group_alpha[0] == pytest.approx(round(0.125 * 255), abs=1)
    _approx_color(surface.get_pixel(0, 0), Color(1, 0, 0, 0.125))
