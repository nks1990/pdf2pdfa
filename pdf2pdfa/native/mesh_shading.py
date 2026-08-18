"""Owned Gouraud mesh shading core for PDF ShadingType 4 and 5.

Type 4 free-form meshes are decoded from edge-flagged vertices. Type 5 lattice
meshes are decoded row-major using ``VerticesPerRow``. Coordinates and color
parameters are bit-packed MSB-first and mapped through the shading ``Decode``
array. Triangles are rasterized in device space with barycentric Gouraud
interpolation and a half-open edge rule so shared edges are not composited
more than once when alpha is active.

Patch meshes (Types 6/7) deliberately remain separate: their bicubic geometry
and tensor/Coons color interpolation should not be approximated as triangles.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math

from .color import ColorSpace, ColorSpaceError, parse_color_space
from .document import PDFDocument
from .function import FunctionError, PDFFunction
from .objects import PDFDict, PDFObject, PDFStream
from .raster import Color, Matrix, Path, Surface, rasterize_fill
from .structure import resolve


class MeshShadingError(ValueError):
    pass


class UnsupportedMeshShadingError(MeshShadingError):
    pass


MAX_VERTICES = 2_000_000
MAX_TRIANGLES = 4_000_000
_ALLOWED_COORD_BITS = {1, 2, 4, 8, 12, 16, 24, 32}
_ALLOWED_COMPONENT_BITS = {1, 2, 4, 8, 12, 16}
_ALLOWED_FLAG_BITS = {2, 4, 8}


@dataclass(frozen=True, slots=True)
class MeshVertex:
    x: float
    y: float
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class MeshTriangle:
    a: MeshVertex
    b: MeshVertex
    c: MeshVertex


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) * 8 - self.position

    def read(self, count: int) -> int:
        if count <= 0 or count > 32:
            raise MeshShadingError(f"invalid mesh bit width {count}")
        if self.remaining < count:
            raise MeshShadingError("truncated mesh shading bitstream")
        value = 0
        for _ in range(count):
            byte = self.data[self.position // 8]
            shift = 7 - (self.position % 8)
            value = (value << 1) | ((byte >> shift) & 1)
            self.position += 1
        return value

    def trailing_zero_padding(self) -> bool:
        while self.remaining:
            if self.read(1):
                return False
        return True


def _dictionary(doc: PDFDocument, value: PDFObject) -> tuple[PDFDict, bytes]:
    resolved = resolve(doc, value)
    if not isinstance(resolved, PDFStream):
        raise MeshShadingError("mesh shading shall be a stream")
    return resolved.dictionary, resolved.data


def _number(doc: PDFDocument, value: PDFObject | None, label: str) -> float:
    if value is None:
        raise MeshShadingError(f"{label} is missing")
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise MeshShadingError(f"{label} shall be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MeshShadingError(f"{label} shall be finite")
    return result


def _integer(doc: PDFDocument, value: PDFObject | None, label: str) -> int:
    value = _number(doc, value, label)
    integer = int(value)
    if integer != value:
        raise MeshShadingError(f"{label} shall be an integer")
    return integer


def _numbers(doc: PDFDocument, value: PDFObject | None, label: str) -> tuple[float, ...]:
    if value is None:
        raise MeshShadingError(f"{label} is missing")
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise MeshShadingError(f"{label} shall be an array")
    return tuple(_number(doc, item, label) for item in value)


def _decode(raw: int, bits: int, lo: float, hi: float) -> float:
    maximum = (1 << bits) - 1
    if maximum <= 0:
        raise MeshShadingError("mesh decode bit range is empty")
    return lo + (raw / maximum) * (hi - lo)


class _MeshColor:
    def __init__(
        self,
        doc: PDFDocument,
        dictionary: PDFDict,
        *,
        resources: PDFDict | None,
    ) -> None:
        try:
            self.space = parse_color_space(
                doc,
                dictionary.get("ColorSpace"),
                resources=resources,
            )
        except ColorSpaceError as exc:
            raise UnsupportedMeshShadingError(str(exc)) from exc
        if self.space.family == "Pattern":
            raise UnsupportedMeshShadingError("Pattern is not a direct mesh color space")

        raw_function = dictionary.get("Function")
        self.functions: tuple[PDFFunction, ...] = ()
        self.function_array = False
        if raw_function is not None:
            raw_function = resolve(doc, raw_function)
            try:
                if isinstance(raw_function, list):
                    if len(raw_function) != self.space.components:
                        raise MeshShadingError(
                            "mesh Function array shall contain one function per color component"
                        )
                    self.functions = tuple(PDFFunction(doc, item) for item in raw_function)
                    self.function_array = True
                else:
                    self.functions = (PDFFunction(doc, raw_function),)
            except FunctionError as exc:
                raise UnsupportedMeshShadingError(str(exc)) from exc
            if any(function.inputs != 1 for function in self.functions):
                raise MeshShadingError("mesh shading Function shall have one input")
            # Validate output cardinality before painting any pixel.
            sample = self.evaluate((0.0,))
            if len(sample) != self.space.components:
                raise MeshShadingError(
                    f"mesh Function produces {len(sample)} component(s), "
                    f"expected {self.space.components}"
                )

    @property
    def encoded_components(self) -> int:
        return 1 if self.functions else self.space.components

    def evaluate(self, values: tuple[float, ...]) -> tuple[float, ...]:
        if self.functions:
            if len(values) != 1:
                raise MeshShadingError("function-based mesh vertex shall carry one parameter")
            try:
                if self.function_array:
                    output: list[float] = []
                    for function in self.functions:
                        result = function.evaluate([values[0]])
                        if len(result) != 1:
                            raise MeshShadingError(
                                "each mesh Function array member shall produce one component"
                            )
                        output.append(result[0])
                    return tuple(output)
                return tuple(self.functions[0].evaluate([values[0]]))
            except FunctionError as exc:
                raise MeshShadingError(f"mesh Function evaluation failed: {exc}") from exc
        if len(values) != self.space.components:
            raise MeshShadingError("mesh vertex color component count is invalid")
        return values

    def rgb(self, values: tuple[float, ...]) -> tuple[float, float, float]:
        try:
            return self.space.rgb(self.evaluate(values))
        except ColorSpaceError as exc:
            raise MeshShadingError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class _MeshSpec:
    shading_type: int
    coord_bits: int
    component_bits: int
    flag_bits: int | None
    vertices_per_row: int | None
    decode: tuple[float, ...]
    color: _MeshColor
    bbox: tuple[float, float, float, float] | None
    background: tuple[float, ...] | None


def _spec(
    doc: PDFDocument,
    dictionary: PDFDict,
    *,
    resources: PDFDict | None,
) -> _MeshSpec:
    shading_type = _integer(doc, dictionary.get("ShadingType"), "ShadingType")
    if shading_type not in (4, 5):
        raise UnsupportedMeshShadingError(
            f"Gouraud mesh core supports ShadingType 4/5, got {shading_type}"
        )
    coord_bits = _integer(doc, dictionary.get("BitsPerCoordinate"), "BitsPerCoordinate")
    component_bits = _integer(doc, dictionary.get("BitsPerComponent"), "BitsPerComponent")
    if coord_bits not in _ALLOWED_COORD_BITS:
        raise MeshShadingError(f"invalid BitsPerCoordinate {coord_bits}")
    if component_bits not in _ALLOWED_COMPONENT_BITS:
        raise MeshShadingError(f"invalid BitsPerComponent {component_bits}")

    flag_bits: int | None = None
    vertices_per_row: int | None = None
    if shading_type == 4:
        flag_bits = _integer(doc, dictionary.get("BitsPerFlag"), "BitsPerFlag")
        if flag_bits not in _ALLOWED_FLAG_BITS:
            raise MeshShadingError(f"invalid BitsPerFlag {flag_bits}")
    else:
        vertices_per_row = _integer(doc, dictionary.get("VerticesPerRow"), "VerticesPerRow")
        if vertices_per_row < 2 or vertices_per_row > MAX_VERTICES:
            raise MeshShadingError("VerticesPerRow shall be between 2 and the owned vertex limit")

    color = _MeshColor(doc, dictionary, resources=resources)
    decode = _numbers(doc, dictionary.get("Decode"), "Mesh/Decode")
    expected = 4 + 2 * color.encoded_components
    if len(decode) != expected:
        raise MeshShadingError(
            f"Mesh/Decode has {len(decode)} values, expected {expected}"
        )

    bbox: tuple[float, float, float, float] | None = None
    if dictionary.get("BBox") is not None:
        values = _numbers(doc, dictionary.get("BBox"), "Mesh/BBox")
        if len(values) != 4:
            raise MeshShadingError("Mesh/BBox shall contain four numbers")
        if values[2] < values[0] or values[3] < values[1]:
            raise MeshShadingError("Mesh/BBox is invalid")
        bbox = (values[0], values[1], values[2], values[3])

    background: tuple[float, ...] | None = None
    if dictionary.get("Background") is not None:
        values = _numbers(doc, dictionary.get("Background"), "Mesh/Background")
        if len(values) != color.space.components:
            raise MeshShadingError(
                "Mesh/Background component count does not match ColorSpace"
            )
        if bbox is None:
            raise MeshShadingError("Mesh/Background requires Mesh/BBox for bounded painting")
        background = values

    return _MeshSpec(
        shading_type,
        coord_bits,
        component_bits,
        flag_bits,
        vertices_per_row,
        decode,
        color,
        bbox,
        background,
    )


def _vertex(reader: _Bits, spec: _MeshSpec) -> MeshVertex:
    x = _decode(
        reader.read(spec.coord_bits),
        spec.coord_bits,
        spec.decode[0],
        spec.decode[1],
    )
    y = _decode(
        reader.read(spec.coord_bits),
        spec.coord_bits,
        spec.decode[2],
        spec.decode[3],
    )
    values: list[float] = []
    for index in range(spec.color.encoded_components):
        base = 4 + index * 2
        values.append(
            _decode(
                reader.read(spec.component_bits),
                spec.component_bits,
                spec.decode[base],
                spec.decode[base + 1],
            )
        )
    return MeshVertex(x, y, tuple(values))


def _type4_triangles(data: bytes, spec: _MeshSpec) -> tuple[MeshTriangle, ...]:
    assert spec.flag_bits is not None
    record_bits = spec.flag_bits + 2 * spec.coord_bits + spec.color.encoded_components * spec.component_bits
    reader = _Bits(data)
    records: list[tuple[int, MeshVertex]] = []
    while reader.remaining >= record_bits:
        if len(records) >= MAX_VERTICES:
            raise UnsupportedMeshShadingError("Type4 mesh vertex count exceeds owned limit")
        flag = reader.read(spec.flag_bits)
        if flag not in (0, 1, 2):
            raise MeshShadingError(f"Type4 mesh edge flag {flag} is invalid")
        records.append((flag, _vertex(reader, spec)))
    if reader.remaining and not reader.trailing_zero_padding():
        raise MeshShadingError("Type4 mesh has non-zero trailing padding bits")

    triangles: list[MeshTriangle] = []
    previous: MeshTriangle | None = None
    index = 0
    while index < len(records):
        flag, current = records[index]
        if flag == 0:
            if index + 2 >= len(records):
                raise MeshShadingError("Type4 flag 0 does not have two following vertices")
            # Flags on the second/third vertex records of a new triangle are
            # carried in the stream but do not select reuse for this triangle.
            triangle = MeshTriangle(current, records[index + 1][1], records[index + 2][1])
            index += 3
        else:
            if previous is None:
                raise MeshShadingError("Type4 reuse flag appears before an initial triangle")
            triangle = (
                MeshTriangle(previous.b, previous.c, current)
                if flag == 1
                else MeshTriangle(previous.a, previous.c, current)
            )
            index += 1
        triangles.append(triangle)
        if len(triangles) > MAX_TRIANGLES:
            raise UnsupportedMeshShadingError("Type4 mesh triangle count exceeds owned limit")
        previous = triangle
    return tuple(triangles)


def _type5_triangles(data: bytes, spec: _MeshSpec) -> tuple[MeshTriangle, ...]:
    assert spec.vertices_per_row is not None
    record_bits = 2 * spec.coord_bits + spec.color.encoded_components * spec.component_bits
    reader = _Bits(data)
    vertices: list[MeshVertex] = []
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
    triangles: list[MeshTriangle] = []
    width = spec.vertices_per_row
    for row in range(rows - 1):
        upper = row * width
        lower = (row + 1) * width
        for column in range(width - 1):
            v00 = vertices[upper + column]
            v01 = vertices[upper + column + 1]
            v10 = vertices[lower + column]
            v11 = vertices[lower + column + 1]
            # PDF lattice cells use the shared v01-v10 diagonal.
            triangles.append(MeshTriangle(v00, v01, v10))
            triangles.append(MeshTriangle(v01, v10, v11))
            if len(triangles) > MAX_TRIANGLES:
                raise UnsupportedMeshShadingError("Type5 mesh triangle count exceeds owned limit")
    return tuple(triangles)


def decode_mesh_triangles(
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


def _bbox_mask(
    surface: Surface,
    ctm: Matrix,
    bbox: tuple[float, float, float, float] | None,
) -> bytearray | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    points = [
        ctm.transform(x0, y0),
        ctm.transform(x1, y0),
        ctm.transform(x1, y1),
        ctm.transform(x0, y1),
    ]
    path = Path()
    path.move_to(*points[0])
    for point in points[1:]:
        path.line_to(*point)
    path.close()
    return rasterize_fill(path, surface.width, surface.height, even_odd=False)


def _edge(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> float:
    return (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])


def _inclusive_edge(a: tuple[float, float], b: tuple[float, float]) -> bool:
    # Half-open edge rule. Reversing the shared edge reverses this predicate, so
    # an exact sample on a triangle seam is owned by exactly one neighbor.
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    return dy > 0 or (abs(dy) <= 1e-15 and dx < 0)


def _inside(value: float, inclusive: bool, eps: float) -> bool:
    return value > eps or (abs(value) <= eps and inclusive)


def _interpolate_values(
    triangle: MeshTriangle,
    wa: float,
    wb: float,
    wc: float,
) -> tuple[float, ...]:
    count = len(triangle.a.values)
    return tuple(
        wa * triangle.a.values[index]
        + wb * triangle.b.values[index]
        + wc * triangle.c.values[index]
        for index in range(count)
    )


def _paint_triangle(
    surface: Surface,
    triangle: MeshTriangle,
    *,
    spec: _MeshSpec,
    ctm: Matrix,
    alpha: float,
    blend_mode: str,
    soft_mask: bytes | bytearray | None,
    bbox_mask: bytearray | None,
) -> None:
    vertices = [
        ctm.transform(triangle.a.x, triangle.a.y),
        ctm.transform(triangle.b.x, triangle.b.y),
        ctm.transform(triangle.c.x, triangle.c.y),
    ]
    colors = [triangle.a, triangle.b, triangle.c]
    area = _edge(vertices[0], vertices[1], vertices[2])
    if abs(area) <= 1e-15:
        return
    if area < 0:
        vertices[1], vertices[2] = vertices[2], vertices[1]
        colors[1], colors[2] = colors[2], colors[1]
        area = -area

    min_x = max(0, math.floor(min(point[0] for point in vertices)))
    max_x = min(surface.width - 1, math.ceil(max(point[0] for point in vertices)) - 1)
    min_y = max(0, math.floor(min(point[1] for point in vertices)))
    max_y = min(surface.height - 1, math.ceil(max(point[1] for point in vertices)) - 1)
    if max_x < min_x or max_y < min_y:
        return

    a, b, c = vertices
    ia = _inclusive_edge(b, c)
    ib = _inclusive_edge(c, a)
    ic = _inclusive_edge(a, b)
    eps = 1e-10 * max(1.0, area)
    logical = MeshTriangle(colors[0], colors[1], colors[2])

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            index = y * surface.width + x
            if surface.clip[index] == 0:
                continue
            if bbox_mask is not None and bbox_mask[index] == 0:
                continue
            p = (x + 0.5, y + 0.5)
            e0 = _edge(b, c, p)
            e1 = _edge(c, a, p)
            e2 = _edge(a, b, p)
            if not (_inside(e0, ia, eps) and _inside(e1, ib, eps) and _inside(e2, ic, eps)):
                continue
            wa, wb, wc = e0 / area, e1 / area, e2 / area
            values = _interpolate_values(logical, wa, wb, wc)
            rgb = spec.color.rgb(values)
            coverage = 1.0 if soft_mask is None else soft_mask[index] / 255.0
            surface.composite_pixel(
                x,
                y,
                Color(rgb[0], rgb[1], rgb[2], alpha),
                coverage=coverage,
                blend_mode=blend_mode,
            )


def paint_mesh_shading(
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
    """Decode and paint one Type4/5 mesh through the current graphics state."""
    if not 0.0 <= fill_alpha <= 1.0:
        raise MeshShadingError("mesh fill alpha shall be between 0 and 1")
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise MeshShadingError("mesh soft-mask dimensions do not match surface")

    spec, triangles = decode_mesh_triangles(doc, shading_value, resources=resources)
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
