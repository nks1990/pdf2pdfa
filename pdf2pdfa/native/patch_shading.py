"""Owned PDF ShadingType 6/7 patch-mesh decoder and painter.

Type 6 Coons patches are converted to an equivalent 4x4 tensor control net.
Type 7 streams provide the full tensor net.  Patch colors are stored at the
four corners and interpolated bilinearly in parameter space, as required by the
PDF patch model.

Rendering uses one uniform power-of-two tessellation level per shading.  The
level is chosen from the maximum device-space departure of the bicubic control
net from its bilinear corner surface.  Uniform subdivision keeps shared patch
edges on identical parameter samples and avoids adaptive T-junction cracks.
Resource limits are explicit and fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .document import PDFDocument
from .mesh_shading import (
    MeshShadingError,
    MeshTriangle,
    MeshVertex,
    UnsupportedMeshShadingError,
    _ALLOWED_COMPONENT_BITS,
    _ALLOWED_COORD_BITS,
    _ALLOWED_FLAG_BITS,
    _Bits,
    _MeshColor,
    _bbox_mask,
    _decode,
    _dictionary,
    _integer,
    _numbers,
    _paint_triangle,
)
from .objects import PDFDict, PDFObject
from .raster import Color, Matrix, Surface


MAX_PATCHES = 25_000
MAX_PATCH_TRIANGLES = 4_000_000
MAX_PATCH_DIVISIONS = 128
MIN_PATCH_DIVISIONS = 8
DEVICE_FLATNESS_TARGET = 0.25


@dataclass(frozen=True, slots=True)
class PatchPoint:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class PatchMesh:
    poles: tuple[tuple[PatchPoint, ...], ...]
    colors: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class _PatchSpec:
    shading_type: int
    coord_bits: int
    component_bits: int
    flag_bits: int
    decode: tuple[float, ...]
    color: _MeshColor
    bbox: tuple[float, float, float, float] | None
    background: tuple[float, ...] | None


def _patch_spec(
    doc: PDFDocument,
    dictionary: PDFDict,
    *,
    resources: PDFDict | None,
) -> _PatchSpec:
    shading_type = _integer(doc, dictionary.get("ShadingType"), "ShadingType")
    if shading_type not in (6, 7):
        raise UnsupportedMeshShadingError(
            f"patch mesh core supports ShadingType 6/7, got {shading_type}"
        )
    coord_bits = _integer(doc, dictionary.get("BitsPerCoordinate"), "BitsPerCoordinate")
    component_bits = _integer(doc, dictionary.get("BitsPerComponent"), "BitsPerComponent")
    flag_bits = _integer(doc, dictionary.get("BitsPerFlag"), "BitsPerFlag")
    if coord_bits not in _ALLOWED_COORD_BITS:
        raise MeshShadingError(f"invalid BitsPerCoordinate {coord_bits}")
    if component_bits not in _ALLOWED_COMPONENT_BITS:
        raise MeshShadingError(f"invalid BitsPerComponent {component_bits}")
    if flag_bits not in _ALLOWED_FLAG_BITS:
        raise MeshShadingError(f"invalid BitsPerFlag {flag_bits}")

    color = _MeshColor(doc, dictionary, resources=resources)
    decode = _numbers(doc, dictionary.get("Decode"), "Patch/Decode")
    expected = 4 + 2 * color.encoded_components
    if len(decode) != expected:
        raise MeshShadingError(
            f"Patch/Decode has {len(decode)} values, expected {expected}"
        )

    bbox: tuple[float, float, float, float] | None = None
    if dictionary.get("BBox") is not None:
        values = _numbers(doc, dictionary.get("BBox"), "Patch/BBox")
        if len(values) != 4:
            raise MeshShadingError("Patch/BBox shall contain four numbers")
        if values[2] < values[0] or values[3] < values[1]:
            raise MeshShadingError("Patch/BBox is invalid")
        bbox = (values[0], values[1], values[2], values[3])

    background: tuple[float, ...] | None = None
    if dictionary.get("Background") is not None:
        values = _numbers(doc, dictionary.get("Background"), "Patch/Background")
        if len(values) != color.space.components:
            raise MeshShadingError(
                "Patch/Background component count does not match ColorSpace"
            )
        if bbox is None:
            raise MeshShadingError("Patch/Background requires Patch/BBox for bounded painting")
        background = values

    return _PatchSpec(
        shading_type,
        coord_bits,
        component_bits,
        flag_bits,
        decode,
        color,
        bbox,
        background,
    )


def _point(reader: _Bits, spec: _PatchSpec) -> PatchPoint:
    return PatchPoint(
        _decode(
            reader.read(spec.coord_bits),
            spec.coord_bits,
            spec.decode[0],
            spec.decode[1],
        ),
        _decode(
            reader.read(spec.coord_bits),
            spec.coord_bits,
            spec.decode[2],
            spec.decode[3],
        ),
    )


def _corner_color(reader: _Bits, spec: _PatchSpec) -> tuple[float, ...]:
    values: list[float] = []
    for index in range(spec.color.encoded_components):
        base = 4 + 2 * index
        values.append(
            _decode(
                reader.read(spec.component_bits),
                spec.component_bits,
                spec.decode[base],
                spec.decode[base + 1],
            )
        )
    return tuple(values)


def _remaining_is_zero(reader: _Bits) -> bool:
    for position in range(reader.position, len(reader.data) * 8):
        byte = reader.data[position // 8]
        shift = 7 - (position % 8)
        if (byte >> shift) & 1:
            return False
    return True


def _coons_interior(
    a: PatchPoint,
    b: PatchPoint,
    c: PatchPoint,
    d: PatchPoint,
    e: PatchPoint,
    f: PatchPoint,
    g: PatchPoint,
    h: PatchPoint,
) -> PatchPoint:
    # Closed-form conversion of a Coons boundary patch to the four missing
    # tensor-product interior poles.  This is the PDF patch equation written
    # symmetrically for x and y.
    def coordinate(name: str) -> float:
        return (
            -4.0 * getattr(a, name)
            + 6.0 * (getattr(b, name) + getattr(c, name))
            - 2.0 * (getattr(d, name) + getattr(e, name))
            + 3.0 * (getattr(f, name) + getattr(g, name))
            - getattr(h, name)
        ) / 9.0

    return PatchPoint(coordinate("x"), coordinate("y"))


def _tensor_poles(shading_type: int, points: list[PatchPoint]) -> tuple[tuple[PatchPoint, ...], ...]:
    expected = 12 if shading_type == 6 else 16
    if len(points) != expected:
        raise MeshShadingError(
            f"ShadingType {shading_type} patch has {len(points)} control points, expected {expected}"
        )

    p: list[list[PatchPoint | None]] = [[None] * 4 for _ in range(4)]
    p[0][0], p[0][1], p[0][2], p[0][3] = points[0:4]
    p[1][3], p[2][3], p[3][3] = points[4:7]
    p[3][2], p[3][1], p[3][0] = points[7:10]
    p[2][0], p[1][0] = points[10:12]

    if shading_type == 7:
        p[1][1], p[1][2], p[2][2], p[2][1] = points[12:16]
    else:
        assert all(value is not None for row in p for value in row if value is not None)
        p[1][1] = _coons_interior(
            p[0][0], p[0][1], p[1][0], p[0][3],
            p[3][0], p[3][1], p[1][3], p[3][3],
        )  # type: ignore[arg-type]
        p[1][2] = _coons_interior(
            p[0][3], p[0][2], p[1][3], p[0][0],
            p[3][3], p[3][2], p[1][0], p[3][0],
        )  # type: ignore[arg-type]
        p[2][1] = _coons_interior(
            p[3][0], p[3][1], p[2][0], p[3][3],
            p[0][0], p[0][1], p[2][3], p[0][3],
        )  # type: ignore[arg-type]
        p[2][2] = _coons_interior(
            p[3][3], p[3][2], p[2][3], p[3][0],
            p[0][3], p[0][2], p[2][0], p[0][0],
        )  # type: ignore[arg-type]

    if any(value is None for row in p for value in row):
        raise MeshShadingError("patch control net is incomplete")
    return tuple(tuple(value for value in row if value is not None) for row in p)


def _reuse_edge(
    flag: int,
    previous_points: list[PatchPoint],
    previous_colors: list[tuple[float, ...]],
) -> tuple[list[PatchPoint], list[tuple[float, ...]]]:
    if flag == 1:
        return previous_points[3:7], [previous_colors[1], previous_colors[2]]
    if flag == 2:
        return previous_points[6:10], [previous_colors[2], previous_colors[3]]
    if flag == 3:
        return (
            [previous_points[9], previous_points[10], previous_points[11], previous_points[0]],
            [previous_colors[3], previous_colors[0]],
        )
    raise MeshShadingError(f"patch edge flag {flag} is invalid")


def decode_patch_mesh(
    doc: PDFDocument,
    shading_value: PDFObject,
    *,
    resources: PDFDict | None = None,
) -> tuple[_PatchSpec, tuple[PatchMesh, ...]]:
    dictionary, data = _dictionary(doc, shading_value)
    spec = _patch_spec(doc, dictionary, resources=resources)
    reader = _Bits(data)
    point_count = 12 if spec.shading_type == 6 else 16
    point_bits = 2 * spec.coord_bits
    color_bits = spec.color.encoded_components * spec.component_bits
    minimum_record_bits = spec.flag_bits + (point_count - 4) * point_bits + 2 * color_bits

    patches: list[PatchMesh] = []
    previous_points: list[PatchPoint] | None = None
    previous_colors: list[tuple[float, ...]] | None = None

    while reader.remaining:
        if reader.remaining < minimum_record_bits:
            if _remaining_is_zero(reader):
                reader.position = len(reader.data) * 8
                break
            raise MeshShadingError("truncated patch mesh record")
        if len(patches) >= MAX_PATCHES:
            raise UnsupportedMeshShadingError("patch mesh count exceeds owned limit")

        flag = reader.read(spec.flag_bits)
        if flag not in (0, 1, 2, 3):
            raise MeshShadingError(f"patch edge flag {flag} is invalid")
        if flag and (previous_points is None or previous_colors is None):
            raise MeshShadingError("patch reuse flag appears before an initial patch")

        points: list[PatchPoint]
        colors: list[tuple[float, ...]]
        if flag == 0:
            points = []
            colors = []
            new_points = point_count
            new_colors = 4
        else:
            assert previous_points is not None and previous_colors is not None
            points, colors = _reuse_edge(flag, previous_points, previous_colors)
            new_points = point_count - 4
            new_colors = 2

        required = new_points * point_bits + new_colors * color_bits
        if reader.remaining < required:
            raise MeshShadingError("truncated patch mesh record payload")
        points.extend(_point(reader, spec) for _ in range(new_points))
        colors.extend(_corner_color(reader, spec) for _ in range(new_colors))

        if len(colors) != 4:
            raise MeshShadingError("patch shall have four corner colors")
        patches.append(PatchMesh(_tensor_poles(spec.shading_type, points), tuple(colors)))
        previous_points = points
        previous_colors = colors

    if not patches:
        raise MeshShadingError("patch mesh contains no complete patch")
    return spec, tuple(patches)


def _bernstein3(t: float) -> tuple[float, float, float, float]:
    t = max(0.0, min(1.0, t))
    s = 1.0 - t
    return (s * s * s, 3.0 * t * s * s, 3.0 * t * t * s, t * t * t)


def _patch_point(patch: PatchMesh, u: float, v: float) -> PatchPoint:
    bu = _bernstein3(u)
    bv = _bernstein3(v)
    x = 0.0
    y = 0.0
    for row in range(4):
        for column in range(4):
            weight = bv[row] * bu[column]
            pole = patch.poles[row][column]
            x += weight * pole.x
            y += weight * pole.y
    return PatchPoint(x, y)


def _patch_values(patch: PatchMesh, u: float, v: float) -> tuple[float, ...]:
    c00, c10, c11, c01 = patch.colors
    return tuple(
        (1.0 - v) * ((1.0 - u) * c00[index] + u * c10[index])
        + v * ((1.0 - u) * c01[index] + u * c11[index])
        for index in range(len(c00))
    )


def _bilerp_point(
    p00: tuple[float, float],
    p10: tuple[float, float],
    p11: tuple[float, float],
    p01: tuple[float, float],
    u: float,
    v: float,
) -> tuple[float, float]:
    return (
        (1.0 - v) * ((1.0 - u) * p00[0] + u * p10[0])
        + v * ((1.0 - u) * p01[0] + u * p11[0]),
        (1.0 - v) * ((1.0 - u) * p00[1] + u * p10[1])
        + v * ((1.0 - u) * p01[1] + u * p11[1]),
    )


def _next_power_of_two(value: int) -> int:
    result = 1
    while result < value:
        result <<= 1
    return result


def _patch_divisions(patches: tuple[PatchMesh, ...], ctm: Matrix) -> int:
    max_deviation = 0.0
    for patch in patches:
        corners = (
            ctm.transform(patch.poles[0][0].x, patch.poles[0][0].y),
            ctm.transform(patch.poles[0][3].x, patch.poles[0][3].y),
            ctm.transform(patch.poles[3][3].x, patch.poles[3][3].y),
            ctm.transform(patch.poles[3][0].x, patch.poles[3][0].y),
        )
        for row in range(4):
            for column in range(4):
                actual = ctm.transform(
                    patch.poles[row][column].x,
                    patch.poles[row][column].y,
                )
                expected = _bilerp_point(
                    corners[0], corners[1], corners[2], corners[3],
                    column / 3.0,
                    row / 3.0,
                )
                max_deviation = max(
                    max_deviation,
                    math.hypot(actual[0] - expected[0], actual[1] - expected[1]),
                )

    geometric = 1 if max_deviation <= DEVICE_FLATNESS_TARGET else math.ceil(
        math.sqrt(max_deviation / DEVICE_FLATNESS_TARGET)
    )
    divisions = max(MIN_PATCH_DIVISIONS, _next_power_of_two(geometric))
    if divisions > MAX_PATCH_DIVISIONS:
        raise UnsupportedMeshShadingError(
            "patch curvature requires subdivisions beyond owned renderer limit"
        )
    triangles = len(patches) * 2 * divisions * divisions
    if triangles > MAX_PATCH_TRIANGLES:
        raise UnsupportedMeshShadingError(
            f"patch tessellation would create {triangles} triangles, exceeding owned limit"
        )
    return divisions


def _mesh_vertex(patch: PatchMesh, u: float, v: float) -> MeshVertex:
    point = _patch_point(patch, u, v)
    return MeshVertex(point.x, point.y, _patch_values(patch, u, v))


def _paint_background(
    surface: Surface,
    *,
    spec: _PatchSpec,
    ctm: Matrix,
    bbox_mask: bytearray | None,
    fill_alpha: float,
    blend_mode: str,
    soft_mask: bytes | bytearray | None,
) -> None:
    if spec.background is None:
        return
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


def paint_patch_shading67(
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
        raise MeshShadingError("patch fill alpha shall be between 0 and 1")
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise MeshShadingError("patch soft-mask dimensions do not match surface")

    spec, patches = decode_patch_mesh(doc, shading_value, resources=resources)
    bbox_mask = _bbox_mask(surface, ctm, spec.bbox)
    _paint_background(
        surface,
        spec=spec,
        ctm=ctm,
        bbox_mask=bbox_mask,
        fill_alpha=fill_alpha,
        blend_mode=blend_mode,
        soft_mask=soft_mask,
    )
    divisions = _patch_divisions(patches, ctm)

    for patch in patches:
        previous = [_mesh_vertex(patch, column / divisions, 0.0) for column in range(divisions + 1)]
        for row in range(1, divisions + 1):
            v = row / divisions
            current = [
                _mesh_vertex(patch, column / divisions, v)
                for column in range(divisions + 1)
            ]
            for column in range(divisions):
                v00 = previous[column]
                v10 = previous[column + 1]
                v01 = current[column]
                v11 = current[column + 1]
                for triangle in (
                    MeshTriangle(v00, v10, v01),
                    MeshTriangle(v10, v01, v11),
                ):
                    _paint_triangle(
                        surface,
                        triangle,
                        spec=spec,  # type: ignore[arg-type]
                        ctm=ctm,
                        alpha=fill_alpha,
                        blend_mode=blend_mode,
                        soft_mask=soft_mask,
                        bbox_mask=bbox_mask,
                    )
            previous = current
