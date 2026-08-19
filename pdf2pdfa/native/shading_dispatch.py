"""Canonical owned shading dispatcher.

All painting entry points (direct ``sh`` operators and PatternType 2 shading
patterns) route through this module so support cannot drift between contexts.

Every shading is first rendered as one intrinsic graphical object onto a
transparent owned surface with Normal blend and unit opacity.  The caller's
alpha, soft mask, blend mode and clip are then applied exactly once at the
shading-object boundary.  This is required for correct Background+mesh
semantics and for knockout groups, where geometric shape must stay distinct
from opacity.
"""

from __future__ import annotations

from .document import PDFDocument
from .function_shading import paint_function_shading
from .mesh_shading import MeshShadingError, UnsupportedMeshShadingError
from .mesh_shading45 import paint_mesh_shading45
from .objects import PDFDict, PDFObject, PDFStream
from .patch_shading import paint_patch_shading67
from .raster import Color, Matrix, Surface
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
    """Materialize general PDF stream filters before a mesh bit parser."""
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


def _paint_intrinsic_shading(
    doc: PDFDocument,
    shading_value: PDFObject,
    shading_type: int,
    *,
    resources: PDFDict | None,
    surface: Surface,
    ctm: Matrix,
) -> None:
    """Paint intrinsic shading color/shape with no outer graphics-state effects."""
    if shading_type == 1:
        paint_function_shading(
            doc,
            shading_value,
            resources=resources,
            surface=surface,
            ctm=ctm,
            fill_alpha=1.0,
            blend_mode="Normal",
            soft_mask=None,
        )
        return
    if shading_type in (2, 3):
        paint_shading(
            doc,
            shading_value,
            resources=resources,
            surface=surface,
            ctm=ctm,
            fill_alpha=1.0,
            blend_mode="Normal",
            soft_mask=None,
        )
        return
    if shading_type in (4, 5):
        mesh_value = _decoded_mesh_stream(doc, shading_value, shading_type)
        try:
            paint_mesh_shading45(
                doc,
                mesh_value,
                resources=resources,
                surface=surface,
                ctm=ctm,
                fill_alpha=1.0,
                blend_mode="Normal",
                soft_mask=None,
            )
        except UnsupportedMeshShadingError as exc:
            raise UnsupportedShadingError(str(exc)) from exc
        except MeshShadingError as exc:
            raise ShadingError(str(exc)) from exc
        return
    if shading_type in (6, 7):
        mesh_value = _decoded_mesh_stream(doc, shading_value, shading_type)
        try:
            paint_patch_shading67(
                doc,
                mesh_value,
                resources=resources,
                surface=surface,
                ctm=ctm,
                fill_alpha=1.0,
                blend_mode="Normal",
                soft_mask=None,
            )
        except UnsupportedMeshShadingError as exc:
            raise UnsupportedShadingError(str(exc)) from exc
        except MeshShadingError as exc:
            raise ShadingError(str(exc)) from exc
        return
    raise UnsupportedShadingError(f"unsupported ShadingType {shading_type}")


def _composite_shading_object(
    destination: Surface,
    intrinsic: Surface,
    *,
    fill_alpha: float,
    blend_mode: str,
    soft_mask: bytes | bytearray | None,
) -> None:
    """Apply outer graphics-state semantics once at the shading boundary.

    ``intrinsic`` alpha represents the shading object's geometric coverage.
    Outer constant alpha and soft-mask samples are source opacity.  Passing
    them in separate ``Color.a`` and ``coverage`` fields is equivalent on a
    normal Surface and remains semantically correct on the owned
    ``KnockoutSurface`` where shape and opacity are intentionally distinct.
    """
    for y in range(destination.height):
        row = y * destination.width
        for x in range(destination.width):
            index = row + x
            source = intrinsic.get_pixel(x, y)
            shape = source.a
            if shape <= 0.0:
                continue
            opacity = fill_alpha
            if soft_mask is not None:
                opacity *= soft_mask[index] / 255.0
            if opacity <= 0.0:
                continue
            destination.composite_pixel(
                x,
                y,
                Color(source.r, source.g, source.b, opacity),
                coverage=shape,
                blend_mode=blend_mode,
            )


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
    """Paint one supported shading as one owned graphical object."""
    if not 0.0 <= fill_alpha <= 1.0:
        raise ShadingError("shading fill alpha shall be between 0 and 1")
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise ShadingError("shading soft-mask dimensions do not match surface")

    shading_type = _shading_type(doc, shading_value)
    intrinsic = Surface(
        surface.width,
        surface.height,
        background=Color(0, 0, 0, 0),
    )
    # Do not copy destination.clip here: it is an outer graphics-state effect
    # and must be applied only once by destination.composite_pixel().
    _paint_intrinsic_shading(
        doc,
        shading_value,
        shading_type,
        resources=resources,
        surface=intrinsic,
        ctm=ctm,
    )
    _composite_shading_object(
        surface,
        intrinsic,
        fill_alpha=fill_alpha,
        blend_mode=blend_mode,
        soft_mask=soft_mask,
    )
