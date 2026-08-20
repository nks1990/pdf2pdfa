"""Corrected owned decoder/paint entry point for ShadingType 4 and 5.

The original mesh geometry/painter primitives live in :mod:`mesh_shading`.
This module owns the stream sequencing rules that are easy to get subtly wrong:

* Type 4 reads a flag for every vertex but only interprets/validates it when a
  new triangle starts;
* Type 4 aligns to the next byte boundary after every vertex record;
* Type 5 remains a continuous bitstream and is triangulated by lattice rows.

Keeping this sequencing layer small makes the rules independently testable
before the common painter is refactored around it.
"""

from __future__ import annotations

from .document import PDFDocument
from .mesh_shading import (
    MAX_TRIANGLES,
    MAX_VERTICES,
    MeshShadingError,
    MeshTriangle,
    UnsupportedMeshShadingError,
    _Bits,
    _MeshSpec,
    _bbox_mask,
    _dictionary,
    _paint_triangle,
    _spec,
    _vertex,
)
from .objects import PDFDict, PDFObject
from .raster import Color, Matrix, Surface


def _align_byte(reader: _Bits) -> None:
    remainder = reader.position % 8
    if remainder:
        reader.position += 8 - remainder
    if reader.position > len(reader.data) * 8:
        raise MeshShadingError("Type4 byte alignment runs past end of stream")


def _type4_triangles(data: bytes, spec: _MeshSpec) -> tuple[MeshTriangle, ...]:
    assert spec.flag_bits is not None
    payload_bits = spec.flag_bits + 2 * spec.coord_bits + spec.color.encoded_components * spec.component_bits
    reader = _Bits(data)
    triangles: list[MeshTriangle] = []
    previous: MeshTriangle | None = None
    pending: list = []
    vertices = 0

    while reader.remaining >= payload_bits:
        if vertices >= MAX_VERTICES:
            raise UnsupportedMeshShadingError("Type4 mesh vertex count exceeds owned limit")
        flag = reader.read(spec.flag_bits)
        vertex = _vertex(reader, spec)
        vertices += 1
        _align_byte(reader)

        if pending:
            # PDF reads the flag field on every vertex record, but while a
            # flag-0 triangle is collecting its second/third vertices those
            # flag values do not control topology and are ignored.
            pending.append(vertex)
            if len(pending) == 3:
                triangle = MeshTriangle(pending[0], pending[1], pending[2])
                triangles.append(triangle)
                previous = triangle
                pending.clear()
            continue

        if flag == 0:
            pending.append(vertex)
            continue
        if flag not in (1, 2):
            raise MeshShadingError(f"Type4 mesh edge flag {flag} is invalid")
        if previous is None:
            raise MeshShadingError("Type4 reuse flag appears before an initial triangle")
        triangle = (
            MeshTriangle(previous.b, previous.c, vertex)
            if flag == 1
            else MeshTriangle(previous.a, previous.c, vertex)
        )
        triangles.append(triangle)
        previous = triangle
        if len(triangles) > MAX_TRIANGLES:
            raise UnsupportedMeshShadingError("Type4 mesh triangle count exceeds owned limit")

    if pending:
        raise MeshShadingError("Type4 flag 0 triangle is truncated")
    if len(triangles) > MAX_TRIANGLES:
        raise UnsupportedMeshShadingError("Type4 mesh triangle count exceeds owned limit")
    if reader.remaining and not reader.trailing_zero_padding():
        raise MeshShadingError("Type4 mesh has non-zero trailing padding bits")
    return tuple(triangles)


def _type5_triangles(data: bytes, spec: _MeshSpec) -> tuple[MeshTriangle, ...]:
    assert spec.vertices_per_row is not None
    record_bits = 2 * spec.coord_bits + spec.color.encoded_components * spec.component_bits
    reader = _Bits(data)
    vertices = []
    while reader.remaining >= record_bits:
        if len(vertices) >= MAX_VERTICES:
            raise UnsupportedMeshShadingError("Type5 mesh vertex count exceeds owned limit")
        vertices.append(_vertex(reader, spec))
    if reader.remaining and not reader.trailing_zero_padding():
        raise MeshShadingError("Type5 mesh has non-zero trailing padding bits")
    if len(vertices) < 2 * spec.vertices_per_row:
        raise MeshShadingError("Type5 mesh requires at least two complete vertex rows")
    if len(vertices) % spec.vertices_per_row:
        raise MeshShadingError("Type5 mesh vertex count is not a multiple of VerticesPerRow")

    rows = len(vertices) // spec.vertices_per_row
    width = spec.vertices_per_row
    triangles: list[MeshTriangle] = []
    for row in range(rows - 1):
        upper = row * width
        lower = (row + 1) * width
        for column in range(width - 1):
            v00 = vertices[upper + column]
            v01 = vertices[upper + column + 1]
            v10 = vertices[lower + column]
            v11 = vertices[lower + column + 1]
            triangles.append(MeshTriangle(v00, v01, v10))
            triangles.append(MeshTriangle(v01, v10, v11))
            if len(triangles) > MAX_TRIANGLES:
                raise UnsupportedMeshShadingError("Type5 mesh triangle count exceeds owned limit")
    return tuple(triangles)


def decode_mesh_triangles45(
    doc: PDFDocument,
    shading_value: PDFObject,
    *,
    resources: PDFDict | None = None,
) -> tuple[_MeshSpec, tuple[MeshTriangle, ...]]:
    dictionary, data = _dictionary(doc, shading_value)
    spec = _spec(doc, dictionary, resources=resources)
    triangles = (
        _type4_triangles(data, spec)
        if spec.shading_type == 4
        else _type5_triangles(data, spec)
    )
    return spec, triangles


def paint_mesh_shading45(
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
    if not 0.0 <= fill_alpha <= 1.0:
        raise MeshShadingError("mesh fill alpha shall be between 0 and 1")
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise MeshShadingError("mesh soft-mask dimensions do not match surface")

    spec, triangles = decode_mesh_triangles45(doc, shading_value, resources=resources)
    bbox_mask = _bbox_mask(surface, ctm, spec.bbox)

    if spec.background is not None:
        assert bbox_mask is not None
        rgb = spec.color.space.rgb(spec.background)
        for y in range(surface.height):
            for x in range(surface.width):
                index = y * surface.width + x
                if surface.clip[index] == 0 or bbox_mask[index] == 0:
                    continue
                coverage = 1.0 if soft_mask is None else soft_mask[index] / 255.0
                surface.composite_pixel(
                    x,
                    y,
                    Color(rgb[0], rgb[1], rgb[2], fill_alpha),
                    coverage=coverage,
                    blend_mode=blend_mode,
                )

    for triangle in triangles:
        _paint_triangle(
            surface,
            triangle,
            spec=spec,
            ctm=ctm,
            alpha=fill_alpha,
            blend_mode=blend_mode,
            soft_mask=soft_mask,
            bbox_mask=bbox_mask,
        )
