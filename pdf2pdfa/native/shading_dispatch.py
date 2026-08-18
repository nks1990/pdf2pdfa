"""Canonical owned shading dispatcher.

All painting entry points (direct ``sh`` operators and PatternType 2 shading
patterns) route through this module so support cannot drift between contexts.

Supported today:

* ShadingType 2/3: axial/radial evaluator;
* ShadingType 4/5: Gouraud free-form/lattice mesh evaluator.

Types 1, 6 and 7 remain explicit fail-closed paths until their owned geometry
and interpolation rules are implemented.
"""

from __future__ import annotations

from .document import PDFDocument
from .mesh_shading import (
    MeshShadingError,
    UnsupportedMeshShadingError,
    paint_mesh_shading,
)
from .objects import PDFDict, PDFObject, PDFStream
from .raster import Matrix, Surface
from .shading import ShadingError, UnsupportedShadingError, paint_shading
from .structure import resolve


def _shading_type(doc: PDFDocument, value: PDFObject) -> int:
    resolved = resolve(doc, value)
    dictionary = resolved.dictionary if isinstance(resolved, PDFStream) else resolved
    if not isinstance(dictionary, PDFDict):
        raise ShadingError("Shading resource is not a dictionary/stream")
    raw = resolve(doc, dictionary.get("ShadingType"))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ShadingError("ShadingType shall be an integer")
    return raw


def paint_owned_shading(
    doc: PDFDocument,
    shading_value: PDFObject,
    *,
    resources: PDFDict | None,
    surface: Surface,
    ctm: Matrix,
    fill_alpha: float = 1.0,
    blend_mode: str = "Normal",
    soft_mask: bytes | bytearray | None = None,
) -> None:
    """Paint one supported shading through the canonical owned implementation."""
    shading_type = _shading_type(doc, shading_value)
    if shading_type in (2, 3):
        paint_shading(
            doc,
            shading_value,
            resources=resources,
            surface=surface,
            ctm=ctm,
            fill_alpha=fill_alpha,
            blend_mode=blend_mode,
            soft_mask=soft_mask,
        )
        return
    if shading_type in (4, 5):
        try:
            paint_mesh_shading(
                doc,
                shading_value,
                resources=resources,
                surface=surface,
                ctm=ctm,
                fill_alpha=fill_alpha,
                blend_mode=blend_mode,
                soft_mask=soft_mask,
            )
        except UnsupportedMeshShadingError as exc:
            raise UnsupportedShadingError(str(exc)) from exc
        except MeshShadingError as exc:
            raise ShadingError(str(exc)) from exc
        return
    if shading_type == 1:
        raise UnsupportedShadingError(
            "ShadingType 1 function-based shading requires the owned function-surface renderer"
        )
    if shading_type in (6, 7):
        raise UnsupportedShadingError(
            f"ShadingType {shading_type} patch mesh requires the owned Coons/tensor patch renderer"
        )
    raise UnsupportedShadingError(f"unsupported ShadingType {shading_type}")
