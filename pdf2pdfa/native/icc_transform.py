"""Pure-Python ICC device-to-PCS evaluation for PDF image rendering."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

from .icc import ICCError, ICCProfile


class ICCTransformError(ICCError):
    pass


_D50 = (0.9642, 1.0, 0.8249)
_XYZ_TO_RGB = (
    (3.1338561, -1.6168667, -0.4906146),
    (-0.9787684, 1.9161415, 0.0334540),
    (0.0719453, -0.2289914, 1.4052427),
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _s15fixed16(data: bytes, offset: int) -> float:
    if offset + 4 > len(data):
        raise ICCTransformError("ICC fixed-point value is truncated")
    return int.from_bytes(data[offset : offset + 4], "big", signed=True) / 65536.0


def _u16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise ICCTransformError("ICC uint16 value is truncated")
    return int.from_bytes(data[offset : offset + 2], "big")


def _xyz_tag(data: bytes) -> tuple[float, float, float]:
    if len(data) < 20 or data[:4] != b"XYZ ":
        raise ICCTransformError("ICC XYZ tag has invalid type/length")
    return (_s15fixed16(data, 8), _s15fixed16(data, 12), _s15fixed16(data, 16))


def _curve(data: bytes) -> Callable[[float], float]:
    if len(data) < 12:
        raise ICCTransformError("ICC curve tag is truncated")
    kind = data[:4]
    if kind == b"curv":
        count = int.from_bytes(data[8:12], "big")
        if count == 0:
            return lambda value: _clamp(value)
        if count == 1:
            if len(data) < 14:
                raise ICCTransformError("ICC gamma curve is truncated")
            gamma = _u16(data, 12) / 256.0
            return lambda value: _clamp(value) ** gamma
        if count > 65535 or 12 + count * 2 > len(data):
            raise ICCTransformError("ICC sampled curve count/length is invalid")
        table = [_u16(data, 12 + 2 * index) / 65535.0 for index in range(count)]

        def sampled(value: float) -> float:
            value = _clamp(value)
            position = value * (count - 1)
            low = int(math.floor(position))
            high = min(count - 1, low + 1)
            fraction = position - low
            return table[low] * (1 - fraction) + table[high] * fraction

        return sampled
    if kind == b"para":
        if len(data) < 12:
            raise ICCTransformError("ICC parametric curve is truncated")
        function_type = _u16(data, 8)
        parameter_counts = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}
        count = parameter_counts.get(function_type)
        if count is None or 12 + 4 * count > len(data):
            raise ICCTransformError(f"unsupported/truncated ICC parametric curve type {function_type}")
        p = [_s15fixed16(data, 12 + 4 * index) for index in range(count)]
        if function_type == 0:
            g, = p
            return lambda x: _clamp(x) ** g
        if function_type == 1:
            g, a, b = p
            return lambda x: (a * x + b) ** g if x >= -b / a else 0.0
        if function_type == 2:
            g, a, b, c = p
            return lambda x: (a * x + b) ** g + c if x >= -b / a else c
        if function_type == 3:
            g, a, b, c, d = p
            return lambda x: (a * x + b) ** g if x >= d else c * x
        g, a, b, c, d, e, f = p
        return lambda x: (a * x + b) ** g + e if x >= d else c * x + f
    raise ICCTransformError(f"unsupported ICC curve type {kind!r}")


def _xyz_to_srgb(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = xyz
    linear = tuple(row[0] * x + row[1] * y + row[2] * z for row in _XYZ_TO_RGB)

    def encode(value: float) -> float:
        value = max(0.0, value)
        if value <= 0.0031308:
            return _clamp(12.92 * value)
        return _clamp(1.055 * (value ** (1 / 2.4)) - 0.055)

    return tuple(encode(value) for value in linear)  # type: ignore[return-value]


def _lab_to_xyz(values: tuple[float, float, float]) -> tuple[float, float, float]:
    # ICC LUT normalized Lab encoding: L*=0..100, a*/b* approximately -128..127.
    lstar = _clamp(values[0]) * 100.0
    astar = _clamp(values[1]) * 255.0 - 128.0
    bstar = _clamp(values[2]) * 255.0 - 128.0
    fy = (lstar + 16.0) / 116.0
    fx = fy + astar / 500.0
    fz = fy - bstar / 200.0
    delta = 6.0 / 29.0

    def inverse(value: float) -> float:
        if value > delta:
            return value ** 3
        return 3 * delta * delta * (value - 4.0 / 29.0)

    return (_D50[0] * inverse(fx), _D50[1] * inverse(fy), _D50[2] * inverse(fz))


def _interpolate_table(table: list[float], value: float) -> float:
    if not table:
        raise ICCTransformError("empty ICC lookup table")
    if len(table) == 1:
        return table[0]
    position = _clamp(value) * (len(table) - 1)
    low = int(math.floor(position))
    high = min(len(table) - 1, low + 1)
    fraction = position - low
    return table[low] * (1 - fraction) + table[high] * fraction


@dataclass(frozen=True, slots=True)
class _LUT:
    input_channels: int
    output_channels: int
    grid_points: int
    input_tables: tuple[tuple[float, ...], ...]
    clut: tuple[float, ...]
    output_tables: tuple[tuple[float, ...], ...]

    def evaluate(self, values: tuple[float, ...]) -> tuple[float, ...]:
        if len(values) != self.input_channels:
            raise ICCTransformError("ICC LUT input channel mismatch")
        coordinates = [
            _interpolate_table(list(self.input_tables[index]), values[index])
            * (self.grid_points - 1)
            for index in range(self.input_channels)
        ]
        lows = [min(self.grid_points - 1, int(math.floor(value))) for value in coordinates]
        highs = [min(self.grid_points - 1, low + 1) for low in lows]
        fractions = [value - low for value, low in zip(coordinates, lows)]
        result = [0.0] * self.output_channels
        # Multilinear interpolation across 2^N corners (N <= 4 in normal PDF ICC use).
        for mask in range(1 << self.input_channels):
            weight = 1.0
            indices: list[int] = []
            for axis in range(self.input_channels):
                if mask & (1 << axis):
                    indices.append(highs[axis])
                    weight *= fractions[axis]
                else:
                    indices.append(lows[axis])
                    weight *= 1.0 - fractions[axis]
            if weight == 0:
                continue
            flat = 0
            for index in indices:
                flat = flat * self.grid_points + index
            base = flat * self.output_channels
            for channel in range(self.output_channels):
                result[channel] += weight * self.clut[base + channel]
        return tuple(
            _interpolate_table(list(self.output_tables[channel]), result[channel])
            for channel in range(self.output_channels)
        )


def _lut8(data: bytes) -> _LUT:
    if len(data) < 48 or data[:4] != b"mft1":
        raise ICCTransformError("invalid ICC lut8 tag")
    input_channels = data[8]
    output_channels = data[9]
    grid = data[10]
    if not (1 <= input_channels <= 15 and 1 <= output_channels <= 15 and grid >= 2):
        raise ICCTransformError("invalid ICC lut8 dimensions")
    position = 48
    input_size = input_channels * 256
    clut_size = (grid ** input_channels) * output_channels
    output_size = output_channels * 256
    if position + input_size + clut_size + output_size > len(data):
        raise ICCTransformError("ICC lut8 payload is truncated")
    input_tables = tuple(
        tuple(value / 255.0 for value in data[position + c * 256 : position + (c + 1) * 256])
        for c in range(input_channels)
    )
    position += input_size
    clut = tuple(value / 255.0 for value in data[position : position + clut_size])
    position += clut_size
    output_tables = tuple(
        tuple(value / 255.0 for value in data[position + c * 256 : position + (c + 1) * 256])
        for c in range(output_channels)
    )
    return _LUT(input_channels, output_channels, grid, input_tables, clut, output_tables)


def _lut16(data: bytes) -> _LUT:
    if len(data) < 52 or data[:4] != b"mft2":
        raise ICCTransformError("invalid ICC lut16 tag")
    input_channels = data[8]
    output_channels = data[9]
    grid = data[10]
    input_entries = _u16(data, 48)
    output_entries = _u16(data, 50)
    if not (
        1 <= input_channels <= 15
        and 1 <= output_channels <= 15
        and grid >= 2
        and input_entries >= 2
        and output_entries >= 2
    ):
        raise ICCTransformError("invalid ICC lut16 dimensions")
    position = 52
    input_size = input_channels * input_entries * 2
    clut_count = (grid ** input_channels) * output_channels
    clut_size = clut_count * 2
    output_size = output_channels * output_entries * 2
    if position + input_size + clut_size + output_size > len(data):
        raise ICCTransformError("ICC lut16 payload is truncated")
    inputs = []
    for channel in range(input_channels):
        start = position + channel * input_entries * 2
        inputs.append(
            tuple(_u16(data, start + index * 2) / 65535.0 for index in range(input_entries))
        )
    position += input_size
    clut = tuple(_u16(data, position + index * 2) / 65535.0 for index in range(clut_count))
    position += clut_size
    outputs = []
    for channel in range(output_channels):
        start = position + channel * output_entries * 2
        outputs.append(
            tuple(_u16(data, start + index * 2) / 65535.0 for index in range(output_entries))
        )
    return _LUT(input_channels, output_channels, grid, tuple(inputs), clut, tuple(outputs))


class ICCDeviceToRGB:
    def __init__(self, profile: ICCProfile) -> None:
        self.profile = profile
        self.components = profile.components
        self._transform = self._build()

    def _build(self):
        profile = self.profile
        if "A2B0" in profile.tags:
            tag = profile.tag_data("A2B0")
            if tag[:4] == b"mft1":
                lut = _lut8(tag)
            elif tag[:4] == b"mft2":
                lut = _lut16(tag)
            else:
                raise ICCTransformError(
                    f"owned ICC engine does not yet evaluate A2B0 type {tag[:4]!r}"
                )
            if lut.input_channels != profile.components or lut.output_channels != 3:
                raise ICCTransformError("ICC A2B0 dimensions do not match profile/PCS")

            def from_lut(values: tuple[float, ...]) -> tuple[float, float, float]:
                pcs = lut.evaluate(values)
                xyz = _lab_to_xyz(pcs) if profile.pcs == "Lab " else pcs
                return _xyz_to_srgb((xyz[0], xyz[1], xyz[2]))

            return from_lut

        if profile.color_space == "RGB ":
            required = ("rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC")
            if not all(tag in profile.tags for tag in required):
                raise ICCTransformError("RGB ICC profile lacks matrix/TRC or A2B0 mapping")
            columns = [_xyz_tag(profile.tag_data(tag)) for tag in ("rXYZ", "gXYZ", "bXYZ")]
            curves = [_curve(profile.tag_data(tag)) for tag in ("rTRC", "gTRC", "bTRC")]

            def matrix(values: tuple[float, ...]) -> tuple[float, float, float]:
                linear = [curves[index](_clamp(values[index])) for index in range(3)]
                xyz = tuple(
                    columns[0][axis] * linear[0]
                    + columns[1][axis] * linear[1]
                    + columns[2][axis] * linear[2]
                    for axis in range(3)
                )
                return _xyz_to_srgb(xyz)  # type: ignore[arg-type]

            return matrix

        if profile.color_space == "GRAY":
            if "kTRC" not in profile.tags:
                raise ICCTransformError("Gray ICC profile lacks kTRC/A2B0")
            curve = _curve(profile.tag_data("kTRC"))
            white = _xyz_tag(profile.tag_data("wtpt")) if "wtpt" in profile.tags else _D50

            def gray(values: tuple[float, ...]) -> tuple[float, float, float]:
                luminance = curve(_clamp(values[0]))
                return _xyz_to_srgb(tuple(component * luminance for component in white))  # type: ignore[arg-type]

            return gray

        raise ICCTransformError(
            f"ICC profile {profile.color_space!r} lacks an owned device-to-PCS transform"
        )

    def __call__(self, values: tuple[float, ...]) -> tuple[float, float, float]:
        if len(values) != self.components:
            raise ICCTransformError(
                f"ICC transform expects {self.components} components, got {len(values)}"
            )
        return self._transform(tuple(_clamp(value) for value in values))
