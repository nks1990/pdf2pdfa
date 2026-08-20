from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdf2pdfa.native.cff_text_render import OwnedOutlineTextRenderer
from pdf2pdfa.native.raster import Matrix, Surface
from pdf2pdfa.native.vertical_metrics import VerticalMetric


def _renderer() -> OwnedOutlineTextRenderer:
    renderer = OwnedOutlineTextRenderer(Surface(8, 8), ctm=Matrix())
    renderer.begin_text()
    renderer.state.font_size = 20.0
    renderer.state.horizontal_scale = 1.0
    renderer.state.font = SimpleNamespace(vertical=True)
    return renderer


def test_vertical_glyph_advance_moves_only_y():
    renderer = _renderer()
    item = SimpleNamespace(
        vertical_metric=VerticalMetric(-1000.0, 300.0, 880.0),
        word_space=False,
    )
    renderer._advance_item(item)
    assert renderer.state.text_matrix.e == pytest.approx(0.0)
    assert renderer.state.text_matrix.f == pytest.approx(-20.0)


def test_vertical_char_spacing_is_added_to_y_and_not_scaled_by_tz():
    renderer = _renderer()
    renderer.state.char_spacing = 3.0
    renderer.state.horizontal_scale = 0.4
    item = SimpleNamespace(
        vertical_metric=VerticalMetric(-1000.0, 300.0, 880.0),
        word_space=False,
    )
    renderer._advance_item(item)
    assert renderer.state.text_matrix.e == pytest.approx(0.0)
    assert renderer.state.text_matrix.f == pytest.approx(-17.0)


def test_vertical_tj_numeric_adjustment_uses_y_axis_without_horizontal_scale():
    renderer = _renderer()
    renderer.state.horizontal_scale = 0.25
    renderer.show_array([250], style=SimpleNamespace())
    assert renderer.state.text_matrix.e == pytest.approx(0.0)
    assert renderer.state.text_matrix.f == pytest.approx(-5.0)


def test_vertical_origin_uses_negative_position_vector_and_tz_only_on_x():
    renderer = _renderer()
    renderer.state.horizontal_scale = 0.5
    renderer.state.rise = 2.0
    item = SimpleNamespace(vertical_metric=VerticalMetric(-1000.0, 400.0, 700.0))
    x, y = renderer._vertical_offset(item)
    assert x == pytest.approx(-4.0)  # -400/1000 * 20 * 0.5
    assert y == pytest.approx(-12.0)  # rise 2 - 700/1000 * 20


def test_horizontal_item_still_uses_original_x_advance():
    renderer = _renderer()
    renderer.state.font = SimpleNamespace(vertical=False)
    renderer.state.horizontal_scale = 0.5
    item = SimpleNamespace(vertical_metric=None, width_1000=600.0, word_space=False)
    # Delegate semantics from the base renderer remain horizontal.
    renderer._advance_item(item)
    assert renderer.state.text_matrix.e == pytest.approx(6.0)
    assert renderer.state.text_matrix.f == pytest.approx(0.0)
