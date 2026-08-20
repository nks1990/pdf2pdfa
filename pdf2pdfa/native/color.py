"""Owned PDF color-space parsing and conversion to renderer sRGB.

The module is shared by images, vector painting, patterns and shadings. Device,
calibrated, Lab, ICCBased, Indexed, Separation and DeviceN spaces are handled
without LittleCMS or platform color APIs. Pattern spaces are intentionally
resolved by the pattern renderer because they carry painting objects, not just
numeric color transforms.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Callable

from .document import PDFDocument
from .function import PDFFunction
from .icc import parse_icc
from .icc_transform import ICCDeviceToRGB
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .structure import decoded_stream_bytes, resolve


class ColorSpaceError(ValueError):
    pass


class UnsupportedColorSpaceError(ColorSpaceError):
    pass


_D50 = (0.9642, 1.0, 0.8249)
_BRADFORD = (
    (0.8951, 0.2664, -0.1614),
    (-0.7502, 1.7135, 0.0367),
    (0.0389, -0.0685, 1.0296),
)
_BRADFORD_INV = (
    (0.9869929, -0.1470543, 0.1599627),
    (0.4323053, 0.5183603, 0.0492912),
    (-0.0085287, 0.0400428, 0.9684867),
)
_XYZ_D50_TO_SRGB = (
    (3.1338561, -1.6168667, -0.4906146),
    (-0.9787684, 1.9161415, 0.0334540),
    (0.0719453, -0.2289914, 1.4052427),
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    try:
        value = resolve(doc, value)
    except Exception:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return default
    return float(value)


def _numbers(doc: PDFDocument, value: PDFObject | None, expected: int, label: str) -> tuple[float, ...]:
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != expected:
        raise ColorSpaceError(f"{label} shall contain {expected} numbers")
    result = tuple(_number(doc, item) for item in value)
    return result


def _mat_vec(matrix, vector):
    return tuple(
        matrix[row][0] * vector[0]
        + matrix[row][1] * vector[1]
        + matrix[row][2] * vector[2]
        for row in range(3)
    )


def _adapt_xyz(
    xyz: tuple[float, float, float],
    source_white: tuple[float, float, float],
    destination_white: tuple[float, float, float] = _D50,
) -> tuple[float, float, float]:
    if all(abs(a - b) < 1e-7 for a, b in zip(source_white, destination_white)):
        return xyz
    source_lms = _mat_vec(_BRADFORD, source_white)
    destination_lms = _mat_vec(_BRADFORD, destination_white)
    value_lms = _mat_vec(_BRADFORD, xyz)
    if any(abs(value) < 1e-12 for value in source_lms):
        raise ColorSpaceError("calibrated color space has degenerate WhitePoint")
    adapted_lms = tuple(
        value_lms[index] * destination_lms[index] / source_lms[index]
        for index in range(3)
    )
    return _mat_vec(_BRADFORD_INV, adapted_lms)  # type: ignore[return-value]


def _xyz_to_srgb(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    linear = _mat_vec(_XYZ_D50_TO_SRGB, xyz)
    def encode(value: float) -> float:
        value = max(0.0, value)
        return _clamp(12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055)
    return tuple(encode(value) for value in linear)  # type: ignore[return-value]


def _device_gray(values: tuple[float, ...]) -> tuple[float, float, float]:
    gray = _clamp(values[0])
    return gray, gray, gray


def _device_rgb(values: tuple[float, ...]) -> tuple[float, float, float]:
    return tuple(_clamp(value) for value in values[:3])  # type: ignore[return-value]


def _device_cmyk(values: tuple[float, ...]) -> tuple[float, float, float]:
    c, m, y, k = (_clamp(value) for value in values[:4])
    return (
        (1.0 - c) * (1.0 - k),
        (1.0 - m) * (1.0 - k),
        (1.0 - y) * (1.0 - k),
    )


@dataclass(frozen=True, slots=True)
class ColorSpace:
    family: str
    components: int
    converter: Callable[[tuple[float, ...]], tuple[float, float, float]]
    indexed_hival: int | None = None

    def rgb(self, values: tuple[float, ...]) -> tuple[float, float, float]:
        if len(values) != self.components:
            raise ColorSpaceError(
                f"/{self.family} expects {self.components} components, got {len(values)}"
            )
        return self.converter(values)

    @property
    def is_indexed(self) -> bool:
        return self.indexed_hival is not None


def _lookup_resource(
    doc: PDFDocument,
    name: str,
    resources: PDFDict | None,
) -> PDFObject | None:
    if resources is None:
        return None
    table = resolve(doc, resources.get("ColorSpace")) if resources.get("ColorSpace") is not None else None
    if isinstance(table, PDFDict) and name in table:
        return table[name]
    return None


def _profile_bytes(doc: PDFDocument, stream: PDFStream) -> bytes:
    return decoded_stream_bytes(doc, stream, label="ICC profile stream")


def parse_color_space(
    doc: PDFDocument,
    value: PDFObject | None,
    *,
    resources: PDFDict | None = None,
    _depth: int = 0,
) -> ColorSpace:
    if _depth > 16:
        raise ColorSpaceError("color-space recursion exceeds 16 levels")
    value = resolve(doc, value)
    if isinstance(value, PDFName):
        name = value.value
        if name in ("DeviceGray", "G"):
            default = _lookup_resource(doc, "DefaultGray", resources)
            if default is not None:
                return parse_color_space(doc, default, resources=resources, _depth=_depth + 1)
            return ColorSpace("DeviceGray", 1, _device_gray)
        if name in ("DeviceRGB", "RGB"):
            default = _lookup_resource(doc, "DefaultRGB", resources)
            if default is not None:
                return parse_color_space(doc, default, resources=resources, _depth=_depth + 1)
            return ColorSpace("DeviceRGB", 3, _device_rgb)
        if name in ("DeviceCMYK", "CMYK"):
            default = _lookup_resource(doc, "DefaultCMYK", resources)
            if default is not None:
                return parse_color_space(doc, default, resources=resources, _depth=_depth + 1)
            return ColorSpace("DeviceCMYK", 4, _device_cmyk)
        resource = _lookup_resource(doc, name, resources)
        if resource is not None:
            return parse_color_space(doc, resource, resources=resources, _depth=_depth + 1)
        if name == "Pattern":
            raise UnsupportedColorSpaceError("Pattern color spaces are resolved by the pattern renderer")
        raise UnsupportedColorSpaceError(f"unresolved color-space name /{name}")

    if not isinstance(value, list) or not value:
        raise ColorSpaceError("ColorSpace is neither a name nor a non-empty array")
    family = _name(resolve(doc, value[0]))

    if family == "CalGray":
        if len(value) != 2:
            raise ColorSpaceError("CalGray array length shall be 2")
        params = resolve(doc, value[1])
        if not isinstance(params, PDFDict):
            raise ColorSpaceError("CalGray parameters are not a dictionary")
        white = _numbers(doc, params.get("WhitePoint"), 3, "CalGray/WhitePoint")
        gamma = _number(doc, params.get("Gamma"), 1.0)
        if gamma <= 0 or white[1] <= 0:
            raise ColorSpaceError("CalGray Gamma/WhitePoint is invalid")
        def calgray(values: tuple[float, ...]) -> tuple[float, float, float]:
            a = _clamp(values[0]) ** gamma
            xyz = (a * white[0], a * white[1], a * white[2])
            return _xyz_to_srgb(_adapt_xyz(xyz, white))
        return ColorSpace(family, 1, calgray)

    if family == "CalRGB":
        if len(value) != 2:
            raise ColorSpaceError("CalRGB array length shall be 2")
        params = resolve(doc, value[1])
        if not isinstance(params, PDFDict):
            raise ColorSpaceError("CalRGB parameters are not a dictionary")
        white = _numbers(doc, params.get("WhitePoint"), 3, "CalRGB/WhitePoint")
        gamma = _numbers(doc, params.get("Gamma"), 3, "CalRGB/Gamma") if params.get("Gamma") is not None else (1.0, 1.0, 1.0)
        matrix_values = _numbers(doc, params.get("Matrix"), 9, "CalRGB/Matrix") if params.get("Matrix") is not None else (1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0)
        if white[1] <= 0 or any(value <= 0 for value in gamma):
            raise ColorSpaceError("CalRGB Gamma/WhitePoint is invalid")
        # PDF Matrix is [XA YA ZA XB YB ZB XC YC ZC].
        matrix = (
            (matrix_values[0], matrix_values[3], matrix_values[6]),
            (matrix_values[1], matrix_values[4], matrix_values[7]),
            (matrix_values[2], matrix_values[5], matrix_values[8]),
        )
        def calrgb(values: tuple[float, ...]) -> tuple[float, float, float]:
            linear = tuple(_clamp(values[index]) ** gamma[index] for index in range(3))
            xyz = _mat_vec(matrix, linear)
            return _xyz_to_srgb(_adapt_xyz(xyz, white))
        return ColorSpace(family, 3, calrgb)

    if family == "Lab":
        if len(value) != 2:
            raise ColorSpaceError("Lab array length shall be 2")
        params = resolve(doc, value[1])
        if not isinstance(params, PDFDict):
            raise ColorSpaceError("Lab parameters are not a dictionary")
        white = _numbers(doc, params.get("WhitePoint"), 3, "Lab/WhitePoint")
        ranges = _numbers(doc, params.get("Range"), 4, "Lab/Range") if params.get("Range") is not None else (-100.0, 100.0, -100.0, 100.0)
        def lab(values: tuple[float, ...]) -> tuple[float, float, float]:
            lstar = _clamp(values[0], 0.0, 100.0)
            astar = _clamp(values[1], ranges[0], ranges[1])
            bstar = _clamp(values[2], ranges[2], ranges[3])
            fy = (lstar + 16.0) / 116.0
            fx = fy + astar / 500.0
            fz = fy - bstar / 200.0
            delta = 6.0 / 29.0
            def inverse(component: float) -> float:
                return component ** 3 if component > delta else 3 * delta * delta * (component - 4 / 29)
            xyz = (white[0] * inverse(fx), white[1] * inverse(fy), white[2] * inverse(fz))
            return _xyz_to_srgb(_adapt_xyz(xyz, white))
        return ColorSpace(family, 3, lab)

    if family == "ICCBased":
        if len(value) != 2:
            raise ColorSpaceError("ICCBased array length shall be 2")
        stream = resolve(doc, value[1])
        if not isinstance(stream, PDFStream):
            raise ColorSpaceError("ICCBased profile is not a stream")
        profile = parse_icc(_profile_bytes(doc, stream))
        transform = ICCDeviceToRGB(profile)
        return ColorSpace(family, profile.components, transform)

    if family == "Indexed":
        if len(value) != 4:
            raise ColorSpaceError("Indexed array length shall be 4")
        base = parse_color_space(doc, value[1], resources=resources, _depth=_depth + 1)
        hival_value = resolve(doc, value[2])
        if isinstance(hival_value, bool) or not isinstance(hival_value, int) or not 0 <= hival_value <= 255:
            raise ColorSpaceError("Indexed hival shall be integer 0..255")
        lookup = resolve(doc, value[3])
        if isinstance(lookup, PDFStream):
            lookup_bytes = decoded_stream_bytes(doc, lookup, label="Indexed lookup")
        elif isinstance(lookup, bytes):
            lookup_bytes = lookup
        else:
            raise ColorSpaceError("Indexed lookup is not a string/stream")
        expected = (hival_value + 1) * base.components
        if len(lookup_bytes) < expected:
            raise ColorSpaceError("Indexed lookup is truncated")
        def indexed(values: tuple[float, ...]) -> tuple[float, float, float]:
            index = int(round(values[0]))
            index = max(0, min(hival_value, index))
            offset = index * base.components
            components = tuple(lookup_bytes[offset + i] / 255.0 for i in range(base.components))
            return base.rgb(components)
        return ColorSpace(family, 1, indexed, indexed_hival=hival_value)

    if family == "Separation":
        if len(value) != 4:
            raise ColorSpaceError("Separation array length shall be 4")
        alternate = parse_color_space(doc, value[2], resources=resources, _depth=_depth + 1)
        function = PDFFunction(doc, value[3])
        def separation(values: tuple[float, ...]) -> tuple[float, float, float]:
            result = tuple(function.evaluate(values))
            if len(result) != alternate.components:
                raise ColorSpaceError("Separation tint transform output count does not match Alternate")
            return alternate.rgb(result)
        return ColorSpace(family, 1, separation)

    if family == "DeviceN":
        if len(value) not in (4, 5):
            raise ColorSpaceError("DeviceN array length shall be 4 or 5")
        names = resolve(doc, value[1])
        if not isinstance(names, list) or not names or not all(isinstance(resolve(doc, item), PDFName) for item in names):
            raise ColorSpaceError("DeviceN colorant names are invalid")
        alternate = parse_color_space(doc, value[2], resources=resources, _depth=_depth + 1)
        function = PDFFunction(doc, value[3])
        components = len(names)
        def devicen(values: tuple[float, ...]) -> tuple[float, float, float]:
            result = tuple(function.evaluate(values))
            if len(result) != alternate.components:
                raise ColorSpaceError("DeviceN tint transform output count does not match Alternate")
            return alternate.rgb(result)
        return ColorSpace(family, components, devicen)

    if family == "Pattern":
        raise UnsupportedColorSpaceError("Pattern color spaces require the pattern renderer")

    raise UnsupportedColorSpaceError(f"unsupported color-space family /{family}")
