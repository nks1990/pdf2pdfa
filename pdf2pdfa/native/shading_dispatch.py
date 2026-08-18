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
from .structure import PDFStructureError, decoded_stream_bytes, resolve


def _resolved_dictionary(doc: PDFDocument, value: PDFObject) -> tuple[PDFObject, PDFDict]:
    resolved = resolve(doc, value)
    dictionary = resolved.dictionary if isinstance(resolved, PDFStream) else resolved
    if not isinstance(dictionary, PDFDict):
        raise ShadingError("Shading resource is not a dictionary/stream")
    return resolved, dictionary


def _shading_type(doc: PDFDocument, value: PDFObject) -> int:
    _, dictionary = _resolved_dictionary(doc, value)
    raw = resolve(doc, dictionary.get("ShadingType"))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ShadingError("ShadingType shall be an integer")
    return raw


def _decoded_mesh_stream(doc: PDFDocument, value: PDFObject, shading_type: int) -> PDFStream:
    """Materialize general PDF stream filters before the mesh bit parser.

    Mesh shading data is itself a PDF stream and may legally be Flate/LZW/etc.
    ``mesh_shading`` deliberately owns only the post-filter bit grammar, while
    this dispatcher owns the generalized stream-filter boundary.
    """
    resolved, _ = _resolved_dictionary(doc, value)
    if not isinstance(resolved, PDFStream):
        raise ShadingError(f"ShadingType {shading_type} mesh shading shall be a stream")
    try:
        payload = decoded_stream_bytes(
            doc,
            resolved,
            label=f"ShadingType {shading_type} mesh stream",
        )
    except (PDFStructureError, ValueError) as exc:
        raise ShadingError(f"cannot decode mesh shading stream: {exc}") from exc

    dictionary = PDFDict(
        {
            key: item
            for key, item in resolved.dictionary.items()
            if key not in {"Filter", "DecodeParms", "DP"}
        }
    )
    return PDFStream(dictionary, payload)


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
        mesh_value = _decoded_mesh_stream(doc, shading_value, shading_type)
        try:
            paint_mesh_shading(
                doc,
                mesh_value,
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
