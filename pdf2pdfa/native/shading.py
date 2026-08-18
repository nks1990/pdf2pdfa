"""Owned ShadingType 2 (axial) and 3 (radial) renderer.

The implementation evaluates PDF Functions and ColorSpaces directly for each
covered device pixel.  Unsupported mesh/function/color paths fail explicitly;
there is no raster/image-library fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math

from .affine_stroke import StrokeError, inverse
from .color import ColorSpace, ColorSpaceError, parse_color_space
from .document import PDFDocument
from .function import FunctionError, PDFFunction
from .objects import PDFDict, PDFObject, PDFStream
from .raster import Color, Matrix, Surface
from .structure import resolve


class ShadingError(ValueError):
    pass


class UnsupportedShadingError(ShadingError):
    pass


def _dictionary(doc: PDFDocument, value: PDFObject, label: str) -> PDFDict:
    value = resolve(doc, value)
    if isinstance(value, PDFStream):
        return value.dictionary
    if not isinstance(value, PDFDict):
        raise ShadingError(f"{label} is not a shading dictionary/stream")
    return value


def _number(doc: PDFDocument, value: PDFObject | None, label: str) -> float:
    if value is None:
        raise ShadingError(f"{label} is missing")
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ShadingError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ShadingError(f"{label} is not finite")
    return result


def _numbers(
    doc: PDFDocument,
    value: PDFObject | None,
    count: int,
    label: str,
) -> tuple[float, ...]:
    if value is None:
        raise ShadingError(f"{label} is missing")
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != count:
        raise ShadingError(f"{label} shall contain {count} numbers")
    return tuple(_number(doc, item, label) for item in value)


def _booleans(
    doc: PDFDocument,
    value: PDFObject | None,
    count: int,
    label: str,
    default: tuple[bool, ...],
) -> tuple[bool, ...]:
    if value is None:
        return default
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != count:
        raise ShadingError(f"{label} shall contain {count} booleans")
    result: list[bool] = []
    for item in value:
        item = resolve(doc, item)
        if not isinstance(item, bool):
            raise ShadingError(f"{label} contains a non-boolean")
        result.append(item)
    return tuple(result)


class _ColorFunction:
    def __init__(
        self,
        doc: PDFDocument,
        value: PDFObject | None,
        components: int,
        domain: tuple[float, float],
    ) -> None:
        if value is None:
            raise ShadingError("axial/radial shading requires /Function")
        value = resolve(doc, value)
        self.functions: list[PDFFunction]
        self.array_mode = isinstance(value, list)
        try:
            if isinstance(value, list):
                if len(value) != components:
                    raise ShadingError(
                        f"shading Function array has {len(value)} entries, expected {components}"
                    )
                self.functions = [PDFFunction(doc, item) for item in value]
            else:
                self.functions = [PDFFunction(doc, value)]
        except FunctionError as exc:
            raise UnsupportedShadingError(str(exc)) from exc
        if any(function.inputs != 1 for function in self.functions):
            raise ShadingError("axial/radial shading functions shall have one input")
        self.components = components
        self.domain = domain
        # Validate output arity once at construction so a malformed function
        # cannot fail after partially painting the surface.
        sample = self.evaluate(domain[0])
        if len(sample) != components:
            raise ShadingError(
                f"shading Function produces {len(sample)} components, expected {components}"
            )

    def evaluate(self, value: float) -> tuple[float, ...]:
        try:
            if self.array_mode:
                output: list[float] = []
                for function in self.functions:
                    result = function.evaluate([value])
                    if len(result) != 1:
                        raise ShadingError(
                            "each function in a shading Function array shall produce one component"
                        )
                    output.append(result[0])
                return tuple(output)
            return tuple(self.functions[0].evaluate([value]))
        except FunctionError as exc:
            raise ShadingError(f"shading Function evaluation failed: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _Common:
    color_space: ColorSpace
    function: _ColorFunction
    domain: tuple[float, float]
    bbox: tuple[float, float, float, float] | None
    background: tuple[float, ...] | None


def _common(
    doc: PDFDocument,
    dictionary: PDFDict,
    *,
    resources: PDFDict | None,
) -> _Common:
    domain = (
        _numbers(doc, dictionary.get("Domain"), 2, "Shading/Domain")
        if dictionary.get("Domain") is not None
        else (0.0, 1.0)
    )
    if domain[0] == domain[1]:
        raise ShadingError("Shading/Domain endpoints shall differ")
    try:
        color_space = parse_color_space(
            doc,
            dictionary.get("ColorSpace"),
            resources=resources,
        )
    except ColorSpaceError as exc:
        raise UnsupportedShadingError(str(exc)) from exc
    if color_space.family == "Pattern":
        raise UnsupportedShadingError("Pattern is not a valid direct shading color space")
    function = _ColorFunction(
        doc,
        dictionary.get("Function"),
        color_space.components,
        domain,
    )
    bbox = None
    if dictionary.get("BBox") is not None:
        bbox = _numbers(doc, dictionary.get("BBox"), 4, "Shading/BBox")
        if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
            raise ShadingError("Shading/BBox is invalid")
    background = None
    if dictionary.get("Background") is not None:
        background = _numbers(
            doc,
            dictionary.get("Background"),
            color_space.components,
            "Shading/Background",
        )
    return _Common(color_space, function, domain, bbox, background)


def _inside_bbox(common: _Common, x: float, y: float) -> bool:
    if common.bbox is None:
        return True
    x0, y0, x1, y1 = common.bbox
    return x0 <= x <= x1 and y0 <= y <= y1


def _background(common: _Common) -> tuple[float, float, float] | None:
    if common.background is None:
        return None
    return common.color_space.rgb(common.background)


def _parameter_color(
    common: _Common,
    normalized: float,
    extend: tuple[bool, bool],
) -> tuple[float, float, float] | None:
    if normalized < 0.0:
        if not extend[0]:
            return _background(common)
        normalized = 0.0
    elif normalized > 1.0:
        if not extend[1]:
            return _background(common)
        normalized = 1.0
    t = common.domain[0] + normalized * (common.domain[1] - common.domain[0])
    values = common.function.evaluate(t)
    try:
        return common.color_space.rgb(values)
    except ColorSpaceError as exc:
        raise ShadingError(str(exc)) from exc


def _radial_parameter(
    x: float,
    y: float,
    coords: tuple[float, float, float, float, float, float],
) -> float | None:
    x0, y0, r0, x1, y1, r1 = coords
    dx = x1 - x0
    dy = y1 - y0
    dr = r1 - r0
    px = x - x0
    py = y - y0
    a = dx * dx + dy * dy - dr * dr
    b = -2.0 * (px * dx + py * dy + r0 * dr)
    c = px * px + py * py - r0 * r0
    eps = 1e-12

    candidates: list[float] = []
    if abs(a) <= eps:
        if abs(b) <= eps:
            return 0.0 if abs(c) <= eps and r0 >= 0 else None
        candidates.append(-c / b)
    else:
        discriminant = b * b - 4.0 * a * c
        if discriminant < -eps:
            return None
        discriminant = max(0.0, discriminant)
        root = math.sqrt(discriminant)
        candidates.extend(((-b - root) / (2.0 * a), (-b + root) / (2.0 * a)))

    # A point can intersect two circles in the interpolated family. PDF radial
    # shadings use the solution whose interpolated radius is positive; when
    # both are geometrically valid the larger parameter represents the visible
    # branch of the two-point cone for the normal radial-gradient construction.
    valid = [t for t in candidates if r0 + t * dr >= -eps and math.isfinite(t)]
    return max(valid) if valid else None


def paint_shading(
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
    """Paint one axial/radial shading through the current user->device CTM."""
    dictionary = _dictionary(doc, shading_value, "Shading resource")
    shading_type = int(_number(doc, dictionary.get("ShadingType"), "ShadingType"))
    if shading_type not in (2, 3):
        raise UnsupportedShadingError(
            f"owned renderer currently supports ShadingType 2/3, got {shading_type}"
        )
    if soft_mask is not None and len(soft_mask) != surface.width * surface.height:
        raise ShadingError("soft-mask dimensions do not match shading surface")
    common = _common(doc, dictionary, resources=resources)
    extend = _booleans(
        doc,
        dictionary.get("Extend"),
        2,
        "Shading/Extend",
        (False, False),
    )
    coords = _numbers(
        doc,
        dictionary.get("Coords"),
        4 if shading_type == 2 else 6,
        "Shading/Coords",
    )
    if shading_type == 2:
        vx = coords[2] - coords[0]
        vy = coords[3] - coords[1]
        length2 = vx * vx + vy * vy
        if length2 <= 1e-24:
            raise ShadingError("axial shading axis has zero length")
    else:
        if coords[2] < 0 or coords[5] < 0:
            raise ShadingError("radial shading radii shall be non-negative")
        if coords[2] == 0 and coords[5] == 0:
            return

    try:
        inv = inverse(ctm)
    except StrokeError as exc:
        raise ShadingError("shading CTM is singular") from exc

    for device_y in range(surface.height):
        for device_x in range(surface.width):
            index = device_y * surface.width + device_x
            if surface.clip[index] == 0:
                continue
            ux, uy = inv.transform(device_x + 0.5, device_y + 0.5)
            if not _inside_bbox(common, ux, uy):
                continue
            if shading_type == 2:
                normalized = (
                    (ux - coords[0]) * (coords[2] - coords[0])
                    + (uy - coords[1]) * (coords[3] - coords[1])
                ) / length2
            else:
                normalized = _radial_parameter(ux, uy, coords)  # type: ignore[arg-type]
                if normalized is None:
                    rgb = _background(common)
                    if rgb is None:
                        continue
                    coverage = 1.0
                    if soft_mask is not None:
                        coverage = soft_mask[index] / 255.0
                    surface.composite_pixel(
                        device_x,
                        device_y,
                        Color(*rgb, fill_alpha),
                        coverage=coverage,
                        blend_mode=blend_mode,
                    )
                    continue
            rgb = _parameter_color(common, normalized, extend)
            if rgb is None:
                continue
            coverage = 1.0
            if soft_mask is not None:
                coverage = soft_mask[index] / 255.0
            surface.composite_pixel(
                device_x,
                device_y,
                Color(*rgb, fill_alpha),
                coverage=coverage,
                blend_mode=blend_mode,
            )
