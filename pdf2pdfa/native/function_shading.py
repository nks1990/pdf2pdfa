"""Owned ShadingType 1 (function-based) renderer.

Function-based shading defines a two-dimensional function domain and a Matrix
that maps that domain coordinate system into the shading target coordinate
space. The common BBox/Background entries remain in target coordinates.
"""

from __future__ import annotations

from .affine_stroke import StrokeError, inverse
from .color import ColorSpaceError, parse_color_space
from .document import PDFDocument
from .function import FunctionError, PDFFunction
from .objects import PDFDict, PDFObject
from .raster import Color, Matrix, Surface
from .shading import ShadingError, UnsupportedShadingError, _dictionary, _number, _numbers
from .structure import resolve


class _Function2D:
    def __init__(self, doc: PDFDocument, value: PDFObject | None, components: int) -> None:
        if value is None:
            raise ShadingError("ShadingType 1 requires /Function")
        value = resolve(doc, value)
        self.array_mode = isinstance(value, list)
        try:
            if isinstance(value, list):
                if len(value) != components:
                    raise ShadingError(
                        f"ShadingType 1 Function array has {len(value)} entries, expected {components}"
                    )
                self.functions = [PDFFunction(doc, item) for item in value]
            else:
                self.functions = [PDFFunction(doc, value)]
        except FunctionError as exc:
            raise UnsupportedShadingError(str(exc)) from exc
        if any(function.inputs != 2 for function in self.functions):
            raise ShadingError("ShadingType 1 functions shall have exactly two inputs")
        self.components = components
        first = self.functions[0]
        sample_x = (first.domain[0] + first.domain[1]) * 0.5
        sample_y = (first.domain[2] + first.domain[3]) * 0.5
        sample = self.evaluate(sample_x, sample_y)
        if len(sample) != components:
            raise ShadingError(
                f"ShadingType 1 Function produces {len(sample)} components, expected {components}"
            )

    def evaluate(self, x: float, y: float) -> tuple[float, ...]:
        try:
            if self.array_mode:
                values = []
                for function in self.functions:
                    result = function.evaluate([x, y])
                    if len(result) != 1:
                        raise ShadingError(
                            "each Function-array entry for ShadingType 1 shall return one component"
                        )
                    values.append(result[0])
                return tuple(values)
            return tuple(self.functions[0].evaluate([x, y]))
        except FunctionError as exc:
            raise ShadingError(f"ShadingType 1 Function evaluation failed: {exc}") from exc


def _matrix(doc: PDFDocument, dictionary: PDFDict) -> Matrix:
    if dictionary.get("Matrix") is None:
        return Matrix()
    values = _numbers(doc, dictionary.get("Matrix"), 6, "Shading/Matrix")
    matrix = Matrix(*values)
    if abs(matrix.a * matrix.d - matrix.b * matrix.c) <= 1e-18:
        raise ShadingError("ShadingType 1 Matrix is singular")
    return matrix


def paint_function_shading(
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
    dictionary = _dictionary(doc, shading_value, "Shading resource")
    shading_type = int(_number(doc, dictionary.get("ShadingType"), "ShadingType"))
    if shading_type != 1:
        raise UnsupportedShadingError(
            f"function shading renderer requires ShadingType 1, got {shading_type}"
        )
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise ShadingError("soft-mask dimensions do not match shading surface")
    if not 0.0 <= fill_alpha <= 1.0:
        raise ShadingError("shading fill alpha shall be between 0 and 1")

    domain = (
        _numbers(doc, dictionary.get("Domain"), 4, "Shading/Domain")
        if dictionary.get("Domain") is not None
        else (0.0, 1.0, 0.0, 1.0)
    )
    x0, x1, y0, y1 = domain
    if x1 <= x0 or y1 <= y0:
        raise ShadingError("ShadingType 1 Domain shall contain increasing x/y bounds")
    xmin, xmax = x0, x1
    ymin, ymax = y0, y1

    try:
        color_space = parse_color_space(
            doc,
            dictionary.get("ColorSpace"),
            resources=resources,
        )
    except ColorSpaceError as exc:
        raise UnsupportedShadingError(str(exc)) from exc
    function = _Function2D(doc, dictionary.get("Function"), color_space.components)

    bbox = None
    if dictionary.get("BBox") is not None:
        bbox = _numbers(doc, dictionary.get("BBox"), 4, "Shading/BBox")
        if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
            raise ShadingError("Shading/BBox is invalid")

    background_rgb = None
    if dictionary.get("Background") is not None:
        values = _numbers(
            doc,
            dictionary.get("Background"),
            color_space.components,
            "Shading/Background",
        )
        try:
            background_rgb = color_space.rgb(values)
        except ColorSpaceError as exc:
            raise ShadingError(str(exc)) from exc

    matrix = _matrix(doc, dictionary)
    try:
        target_to_user = inverse(ctm)
        function_to_device = ctm.concat(matrix)
        device_to_function = inverse(function_to_device)
    except StrokeError as exc:
        raise ShadingError("ShadingType 1 transform is singular") from exc

    for device_y in range(surface.height):
        for device_x in range(surface.width):
            index = device_y * surface.width + device_x
            if surface.clip[index] == 0:
                continue
            px = device_x + 0.5
            py = device_y + 0.5
            tx, ty = target_to_user.transform(px, py)
            if bbox is not None and not (bbox[0] <= tx <= bbox[2] and bbox[1] <= ty <= bbox[3]):
                continue

            fx, fy = device_to_function.transform(px, py)
            if xmin <= fx <= xmax and ymin <= fy <= ymax:
                try:
                    rgb = color_space.rgb(function.evaluate(fx, fy))
                except ColorSpaceError as exc:
                    raise ShadingError(str(exc)) from exc
            else:
                if background_rgb is None:
                    continue
                rgb = background_rgb

            coverage = 1.0 if soft_mask is None else soft_mask[index] / 255.0
            surface.composite_pixel(
                device_x,
                device_y,
                Color(rgb[0], rgb[1], rgb[2], fill_alpha),
                coverage=coverage,
                blend_mode=blend_mode,
            )
