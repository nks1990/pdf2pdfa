from __future__ import annotations

import pytest

from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError

from tests.native.test_function_shading import _pdf, _shading


def test_function_shading_reversed_x_domain_is_fail_closed():
    shading = _shading()
    shading["Domain"] = [1, 0, 0, 1]
    with pytest.raises(RenderingError, match="increasing x/y bounds"):
        render_page_full(_pdf(shading), dpi=72)


def test_function_shading_reversed_y_domain_is_fail_closed():
    shading = _shading()
    shading["Domain"] = [0, 1, 1, 0]
    with pytest.raises(RenderingError, match="increasing x/y bounds"):
        render_page_full(_pdf(shading), dpi=72)


def test_function_shading_singular_matrix_is_fail_closed():
    shading = _shading(matrix=(1, 0, 2, 0, 0, 0))
    with pytest.raises(RenderingError, match="Matrix is singular"):
        render_page_full(_pdf(shading), dpi=72)
