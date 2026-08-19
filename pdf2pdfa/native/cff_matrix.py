"""Owned CFF FontMatrix composition for CID-keyed per-FD dictionaries.

CFF CID fonts may select a Font DICT per glyph through FDSelect.  When that
Font DICT supplies its own FontMatrix, mature CFF consumers concatenate the
top-level linear matrix with the FD matrix and transform the FD offset by the
top-level linear matrix.  The top-level offset is inherited only when the FD
has no FontMatrix of its own.

Keeping this rule in one tiny module makes the non-obvious translation
semantics testable independently from PDF font dictionaries and Type2 outline
interpretation.
"""

from __future__ import annotations

from collections.abc import Sequence
import math

from .raster import Matrix


class CFFMatrixError(ValueError):
    pass


def _matrix(values: Sequence[float], *, label: str) -> Matrix:
    if len(values) != 6:
        raise CFFMatrixError(f"{label} shall contain six numbers")
    numbers = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in numbers):
        raise CFFMatrixError(f"{label} contains a non-finite number")
    result = Matrix(*numbers)
    if abs(result.a * result.d - result.b * result.c) <= 1e-18:
        raise CFFMatrixError(f"{label} is singular")
    return result


def effective_cid_font_matrix(
    top_values: Sequence[float],
    fd_values: Sequence[float] | None,
) -> Matrix:
    """Return the effective CFF matrix for one CID-keyed glyph.

    If the selected FD has no FontMatrix, the complete top-level matrix is
    inherited.  If it has a matrix, concatenate only the top-level linear part
    with the FD affine transform.  This mirrors the CFF reference behavior:
    the FD offset is transformed by the top matrix's 2x2 component rather than
    receiving the top-level translation a second time.
    """
    top = _matrix(top_values, label="CFF top FontMatrix")
    if fd_values is None:
        return top
    fd = _matrix(fd_values, label="CFF FD FontMatrix")

    top_linear = Matrix(top.a, top.b, top.c, top.d, 0.0, 0.0)
    return top_linear.concat(fd)
