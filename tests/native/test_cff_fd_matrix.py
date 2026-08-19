from __future__ import annotations

import pytest

from pdf2pdfa.native.cff_matrix import CFFMatrixError, effective_cid_font_matrix
from pdf2pdfa.native.raster import Matrix


def test_no_fd_matrix_inherits_complete_top_affine_matrix():
    result = effective_cid_font_matrix(
        (0.002, 0.001, -0.001, 0.003, 7.0, -4.0),
        None,
    )
    assert result == Matrix(0.002, 0.001, -0.001, 0.003, 7.0, -4.0)


def test_fd_linear_matrix_is_postconcatenated_after_top_linear_matrix():
    result = effective_cid_font_matrix(
        (2.0, 1.0, 0.5, 3.0, 100.0, 200.0),
        (4.0, -1.0, 2.0, 5.0, 0.0, 0.0),
    )
    # top.linear * fd.linear; this deliberately uses non-commuting matrices.
    assert result == Matrix(
        7.5, 1.0,
        6.5, 17.0,
        0.0, 0.0,
    )


def test_fd_offset_is_transformed_by_top_linear_without_top_translation_addition():
    result = effective_cid_font_matrix(
        (2.0, 1.0, -1.0, 3.0, 100.0, 200.0),
        (1.0, 0.0, 0.0, 1.0, 10.0, -5.0),
    )
    # (10,-5) through the top 2x2 gives (25,-5). The top (100,200)
    # translation is intentionally not added when the FD owns FontMatrix.
    assert result == Matrix(2.0, 1.0, -1.0, 3.0, 25.0, -5.0)


def test_default_cff_top_matrix_combines_with_fd_scale():
    result = effective_cid_font_matrix(
        (0.001, 0.0, 0.0, 0.001, 0.0, 0.0),
        (2.0, 0.0, 0.0, 2.0, 0.0, 0.0),
    )
    assert result == Matrix(0.002, 0.0, 0.0, 0.002, 0.0, 0.0)


@pytest.mark.parametrize(
    "top, fd, message",
    [
        ((1, 0, 0, 0, 0, 0), None, "top FontMatrix is singular"),
        ((1, 0, 0, 1, 0, 0), (0, 0, 0, 1, 0, 0), "FD FontMatrix is singular"),
        ((1, 0, 0, 1, 0, 0), (1, 0, 0, 1, float("inf"), 0), "non-finite"),
    ],
)
def test_invalid_effective_cff_matrix_fails_closed(top, fd, message):
    with pytest.raises(CFFMatrixError, match=message):
        effective_cid_font_matrix(top, fd)
