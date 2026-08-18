from __future__ import annotations

from pdf2pdfa.native.raster import Color, Matrix, Path, Surface, blend_rgb


def test_matrix_concat_and_transform():
    scale = Matrix(2, 0, 0, 3, 0, 0)
    translate = Matrix(1, 0, 0, 1, 5, 7)
    combined = translate.concat(scale)
    assert combined.transform(2, 4) == (9, 19)


def test_normal_alpha_composite_over_white():
    surface = Surface(1, 1, background=Color(1, 1, 1, 1))
    surface.composite_pixel(0, 0, Color(1, 0, 0, 0.5))
    pixel = surface.get_pixel(0, 0)
    assert abs(pixel.r - 1.0) < 0.01
    assert abs(pixel.g - 0.5) < 0.02
    assert abs(pixel.b - 0.5) < 0.02
    assert pixel.a == 1.0


def test_multiply_blend_mode():
    result = blend_rgb((0.5, 0.25, 1.0), (0.5, 0.5, 0.5), "Multiply")
    assert result == (0.25, 0.125, 0.5)


def test_nonseparable_blend_modes_stay_in_gamut():
    backdrop = (0.2, 0.8, 0.4)
    source = (0.9, 0.1, 0.5)
    for mode in ("Hue", "Saturation", "Color", "Luminosity"):
        result = blend_rgb(backdrop, source, mode)
        assert all(0.0 <= value <= 1.0 for value in result)


def test_fill_rectangle_produces_expected_area():
    surface = Surface(10, 10)
    path = Path()
    path.rectangle(2, 2, 4, 3)
    surface.fill_path(path, Color(1, 0, 0, 1))
    opaque = 0
    for y in range(10):
        for x in range(10):
            if surface.get_pixel(x, y).a > 0.5:
                opaque += 1
    assert opaque == 12


def test_stroke_butt_cap_does_not_extend_past_endpoints():
    surface = Surface(12, 8)
    path = Path()
    path.move_to(3, 4)
    path.line_to(8, 4)
    surface.stroke_path(path, Color(0, 0, 0, 1), stroke_width=2, line_cap=0)
    assert surface.get_pixel(2, 4).a == 0
    assert surface.get_pixel(3, 4).a > 0
    assert surface.get_pixel(8, 4).a == 0


def test_clip_mask_blocks_pixels():
    surface = Surface(4, 1)
    surface.apply_clip_mask(bytes([255, 0, 255, 0]))
    for x in range(4):
        surface.composite_pixel(x, 0, Color(1, 0, 0, 1))
    assert [surface.get_pixel(x, 0).a for x in range(4)] == [1.0, 0.0, 1.0, 0.0]
