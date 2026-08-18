"""Pure-Python raster surface, vector fill/stroke and PDF blend modes."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable, Iterator, Literal, Sequence


class RasterError(ValueError):
    pass


BlendMode = Literal[
    "Normal", "Compatible", "Multiply", "Screen", "Overlay", "Darken", "Lighten",
    "ColorDodge", "ColorBurn", "HardLight", "SoftLight", "Difference", "Exclusion",
    "Hue", "Saturation", "Color", "Luminosity",
]


@dataclass(frozen=True, slots=True)
class Matrix:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def transform(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def concat(self, other: "Matrix") -> "Matrix":
        """Return self * other in PDF affine convention."""
        return Matrix(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            e=self.a * other.e + self.c * other.f + self.e,
            f=self.b * other.e + self.d * other.f + self.f,
        )

    @property
    def expansion(self) -> float:
        # Geometric mean scale, stable for rotations/skews and adequate for line width.
        determinant = abs(self.a * self.d - self.b * self.c)
        return math.sqrt(determinant) if determinant > 0 else 0.0


IDENTITY = Matrix()


@dataclass(frozen=True, slots=True)
class Color:
    r: float
    g: float
    b: float
    a: float = 1.0

    def clamped(self) -> "Color":
        return Color(*(_clamp(value) for value in (self.r, self.g, self.b, self.a)))

    @classmethod
    def gray(cls, value: float, alpha: float = 1.0) -> "Color":
        value = _clamp(value)
        return cls(value, value, value, _clamp(alpha))

    @classmethod
    def rgb(cls, r: float, g: float, b: float, alpha: float = 1.0) -> "Color":
        return cls(_clamp(r), _clamp(g), _clamp(b), _clamp(alpha))

    @classmethod
    def cmyk(cls, c: float, m: float, y: float, k: float, alpha: float = 1.0) -> "Color":
        c, m, y, k = (_clamp(value) for value in (c, m, y, k))
        return cls(
            (1.0 - c) * (1.0 - k),
            (1.0 - m) * (1.0 - k),
            (1.0 - y) * (1.0 - k),
            _clamp(alpha),
        )


@dataclass(slots=True)
class Subpath:
    points: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False


@dataclass(slots=True)
class Path:
    subpaths: list[Subpath] = field(default_factory=list)
    _current: Subpath | None = None

    def move_to(self, x: float, y: float) -> None:
        subpath = Subpath([(x, y)], False)
        self.subpaths.append(subpath)
        self._current = subpath

    def _require_current(self) -> Subpath:
        if self._current is None or not self._current.points:
            raise RasterError("path operation requires a current point")
        return self._current

    @property
    def current_point(self) -> tuple[float, float]:
        return self._require_current().points[-1]

    def line_to(self, x: float, y: float) -> None:
        self._require_current().points.append((x, y))

    def curve_to(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        x3: float,
        y3: float,
        *,
        tolerance: float = 0.25,
    ) -> None:
        p0 = self.current_point
        points: list[tuple[float, float]] = []
        _flatten_cubic(p0, (x1, y1), (x2, y2), (x3, y3), tolerance, points, 0)
        self._require_current().points.extend(points)

    def close(self) -> None:
        current = self._require_current()
        current.closed = True

    def rectangle(self, x: float, y: float, width: float, height: float) -> None:
        self.move_to(x, y)
        self.line_to(x + width, y)
        self.line_to(x + width, y + height)
        self.line_to(x, y + height)
        self.close()

    def clear(self) -> None:
        self.subpaths.clear()
        self._current = None

    def transformed(self, matrix: Matrix) -> "Path":
        result = Path()
        for subpath in self.subpaths:
            if not subpath.points:
                continue
            first = matrix.transform(*subpath.points[0])
            result.move_to(*first)
            for point in subpath.points[1:]:
                result.line_to(*matrix.transform(*point))
            if subpath.closed:
                result.close()
        return result


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def _distance_point_line(point, start, end) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    denominator = math.hypot(dx, dy)
    if denominator == 0:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / denominator


def _flatten_cubic(p0, p1, p2, p3, tolerance, output, depth) -> None:
    if depth >= 16 or max(
        _distance_point_line(p1, p0, p3),
        _distance_point_line(p2, p0, p3),
    ) <= tolerance:
        output.append(p3)
        return
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p23 = _mid(p2, p3)
    p012 = _mid(p01, p12)
    p123 = _mid(p12, p23)
    p0123 = _mid(p012, p123)
    _flatten_cubic(p0, p01, p012, p0123, tolerance, output, depth + 1)
    _flatten_cubic(p0123, p123, p23, p3, tolerance, output, depth + 1)


def _mid(a, b):
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def _lum(c: tuple[float, float, float]) -> float:
    return 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]


def _sat(c: tuple[float, float, float]) -> float:
    return max(c) - min(c)


def _clip_color(c: tuple[float, float, float]) -> tuple[float, float, float]:
    l = _lum(c)
    n = min(c)
    x = max(c)
    values = list(c)
    if n < 0:
        values = [l + ((value - l) * l / (l - n)) for value in values]
    x = max(values)
    if x > 1:
        values = [l + ((value - l) * (1 - l) / (x - l)) for value in values]
    return tuple(_clamp(value) for value in values)  # type: ignore[return-value]


def _set_lum(c: tuple[float, float, float], l: float) -> tuple[float, float, float]:
    delta = l - _lum(c)
    return _clip_color(tuple(value + delta for value in c))  # type: ignore[arg-type]


def _set_sat(c: tuple[float, float, float], saturation: float) -> tuple[float, float, float]:
    indexed = sorted(enumerate(c), key=lambda item: item[1])
    i_min, v_min = indexed[0]
    i_mid, v_mid = indexed[1]
    i_max, v_max = indexed[2]
    result = [0.0, 0.0, 0.0]
    if v_max > v_min:
        result[i_mid] = ((v_mid - v_min) * saturation) / (v_max - v_min)
        result[i_max] = saturation
    result[i_min] = 0.0
    return tuple(result)  # type: ignore[return-value]


def _blend_channel(backdrop: float, source: float, mode: str) -> float:
    if mode in ("Normal", "Compatible"):
        return source
    if mode == "Multiply":
        return backdrop * source
    if mode == "Screen":
        return backdrop + source - backdrop * source
    if mode == "Overlay":
        return 2 * backdrop * source if backdrop <= 0.5 else 1 - 2 * (1 - backdrop) * (1 - source)
    if mode == "Darken":
        return min(backdrop, source)
    if mode == "Lighten":
        return max(backdrop, source)
    if mode == "ColorDodge":
        if source >= 1:
            return 1.0
        return min(1.0, backdrop / (1.0 - source))
    if mode == "ColorBurn":
        if source <= 0:
            return 0.0
        return 1.0 - min(1.0, (1.0 - backdrop) / source)
    if mode == "HardLight":
        return 2 * backdrop * source if source <= 0.5 else 1 - 2 * (1 - backdrop) * (1 - source)
    if mode == "SoftLight":
        if source <= 0.5:
            return backdrop - (1 - 2 * source) * backdrop * (1 - backdrop)
        d = ((16 * backdrop - 12) * backdrop + 4) * backdrop if backdrop <= 0.25 else math.sqrt(backdrop)
        return backdrop + (2 * source - 1) * (d - backdrop)
    if mode == "Difference":
        return abs(backdrop - source)
    if mode == "Exclusion":
        return backdrop + source - 2 * backdrop * source
    raise RasterError(f"unsupported separable blend mode {mode}")


def blend_rgb(
    backdrop: tuple[float, float, float],
    source: tuple[float, float, float],
    mode: str,
) -> tuple[float, float, float]:
    if mode == "Hue":
        return _set_lum(_set_sat(source, _sat(backdrop)), _lum(backdrop))
    if mode == "Saturation":
        return _set_lum(_set_sat(backdrop, _sat(source)), _lum(backdrop))
    if mode == "Color":
        return _set_lum(source, _lum(backdrop))
    if mode == "Luminosity":
        return _set_lum(backdrop, _lum(source))
    return tuple(_clamp(_blend_channel(b, s, mode)) for b, s in zip(backdrop, source))  # type: ignore[return-value]


class Surface:
    """Straight-alpha 8-bit RGBA canvas with optional clip coverage."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        background: Color = Color(0.0, 0.0, 0.0, 0.0),
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("surface dimensions must be positive")
        if width * height > 250_000_000:
            raise ValueError("surface is too large")
        self.width = width
        self.height = height
        bg = background.clamped()
        pixel = bytes(
            [
                round(bg.r * 255),
                round(bg.g * 255),
                round(bg.b * 255),
                round(bg.a * 255),
            ]
        )
        self.pixels = bytearray(pixel * (width * height))
        self.clip = bytearray(b"\xff" * (width * height))

    def copy(self) -> "Surface":
        other = Surface(self.width, self.height)
        other.pixels[:] = self.pixels
        other.clip[:] = self.clip
        return other

    def clear(self, color: Color = Color(0, 0, 0, 0)) -> None:
        c = color.clamped()
        pixel = bytes([round(c.r * 255), round(c.g * 255), round(c.b * 255), round(c.a * 255)])
        self.pixels[:] = pixel * (self.width * self.height)

    def _offset(self, x: int, y: int) -> int:
        return (y * self.width + x) * 4

    def get_pixel(self, x: int, y: int) -> Color:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError((x, y))
        offset = self._offset(x, y)
        return Color(*(self.pixels[offset + i] / 255.0 for i in range(4)))

    def composite_pixel(
        self,
        x: int,
        y: int,
        source: Color,
        *,
        coverage: float = 1.0,
        blend_mode: str = "Normal",
    ) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        coverage = _clamp(coverage)
        clip = self.clip[y * self.width + x] / 255.0
        source = source.clamped()
        alpha_s = source.a * coverage * clip
        if alpha_s <= 0:
            return
        offset = self._offset(x, y)
        cb = tuple(self.pixels[offset + index] / 255.0 for index in range(3))
        alpha_b = self.pixels[offset + 3] / 255.0
        cs = (source.r, source.g, source.b)
        blended = blend_rgb(cb, cs, blend_mode)
        alpha_r = alpha_s + alpha_b * (1.0 - alpha_s)
        if alpha_r <= 0:
            result = (0.0, 0.0, 0.0)
        else:
            result = tuple(
                (
                    (1.0 - alpha_s) * alpha_b * cb[index]
                    + (1.0 - alpha_b) * alpha_s * cs[index]
                    + alpha_b * alpha_s * blended[index]
                )
                / alpha_r
                for index in range(3)
            )
        for index, value in enumerate(result):
            self.pixels[offset + index] = round(_clamp(value) * 255)
        self.pixels[offset + 3] = round(_clamp(alpha_r) * 255)

    def composite_surface(
        self,
        source: "Surface",
        x: int = 0,
        y: int = 0,
        *,
        alpha: float = 1.0,
        blend_mode: str = "Normal",
        mask: bytes | bytearray | None = None,
    ) -> None:
        for sy in range(source.height):
            dy = y + sy
            if dy < 0 or dy >= self.height:
                continue
            for sx in range(source.width):
                dx = x + sx
                if dx < 0 or dx >= self.width:
                    continue
                src = source.get_pixel(sx, sy)
                coverage = alpha
                if mask is not None:
                    coverage *= mask[sy * source.width + sx] / 255.0
                self.composite_pixel(dx, dy, src, coverage=coverage, blend_mode=blend_mode)

    def flatten_onto(self, background: Color = Color(1, 1, 1, 1)) -> "Surface":
        result = Surface(self.width, self.height, background=background)
        result.composite_surface(self)
        return result

    def rgb_bytes(self, *, flatten: Color = Color(1, 1, 1, 1)) -> bytes:
        source = self.flatten_onto(flatten) if any(self.pixels[index] != 255 for index in range(3, len(self.pixels), 4)) else self
        out = bytearray(self.width * self.height * 3)
        target = 0
        for offset in range(0, len(source.pixels), 4):
            out[target : target + 3] = source.pixels[offset : offset + 3]
            target += 3
        return bytes(out)

    def apply_clip_mask(self, mask: bytes | bytearray) -> None:
        if len(mask) != self.width * self.height:
            raise ValueError("clip mask dimensions do not match surface")
        for index, value in enumerate(mask):
            self.clip[index] = (self.clip[index] * value + 127) // 255

    def fill_path(
        self,
        path: Path,
        color: Color,
        *,
        even_odd: bool = False,
        blend_mode: str = "Normal",
    ) -> None:
        mask = rasterize_fill(path, self.width, self.height, even_odd=even_odd)
        for index, coverage in enumerate(mask):
            if not coverage:
                continue
            y, x = divmod(index, self.width)
            self.composite_pixel(
                x,
                y,
                color,
                coverage=coverage / 255.0,
                blend_mode=blend_mode,
            )

    def stroke_path(
        self,
        path: Path,
        color: Color,
        *,
        width: float = 1.0,
        line_cap: int = 0,
        line_join: int = 0,
        blend_mode: str = "Normal",
    ) -> None:
        mask = rasterize_stroke(
            path,
            self.width,
            self.height,
            width=max(0.01, width),
            line_cap=line_cap,
            line_join=line_join,
        )
        for index, coverage in enumerate(mask):
            if not coverage:
                continue
            y, x = divmod(index, self.width)
            self.composite_pixel(
                x,
                y,
                color,
                coverage=coverage / 255.0,
                blend_mode=blend_mode,
            )


def _edges(path: Path, *, close_open: bool) -> Iterator[tuple[float, float, float, float, int]]:
    for subpath in path.subpaths:
        points = subpath.points
        if len(points) < 2:
            continue
        count = len(points) if (subpath.closed or close_open) else len(points) - 1
        for index in range(count):
            x1, y1 = points[index]
            x2, y2 = points[(index + 1) % len(points)]
            if y1 == y2:
                continue
            winding = 1 if y2 > y1 else -1
            yield x1, y1, x2, y2, winding


def rasterize_fill(path: Path, width: int, height: int, *, even_odd: bool = False) -> bytearray:
    mask = bytearray(width * height)
    edges = list(_edges(path, close_open=True))
    if not edges:
        return mask
    min_y = max(0, math.floor(min(min(y1, y2) for _, y1, _, y2, _ in edges)))
    max_y = min(height - 1, math.ceil(max(max(y1, y2) for _, y1, _, y2, _ in edges)))
    for y in range(min_y, max_y + 1):
        scan_y = y + 0.5
        intersections: list[tuple[float, int]] = []
        for x1, y1, x2, y2, winding in edges:
            low, high = (y1, y2) if y1 < y2 else (y2, y1)
            if not (low <= scan_y < high):
                continue
            x = x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1)
            intersections.append((x, winding))
        intersections.sort(key=lambda item: item[0])
        if even_odd:
            for index in range(0, len(intersections) - 1, 2):
                _fill_span(mask, width, y, intersections[index][0], intersections[index + 1][0])
        else:
            winding = 0
            start: float | None = None
            for x, delta in intersections:
                previous = winding
                winding += delta
                if previous == 0 and winding != 0:
                    start = x
                elif previous != 0 and winding == 0 and start is not None:
                    _fill_span(mask, width, y, start, x)
                    start = None
    return mask


def _fill_span(mask: bytearray, width: int, y: int, x1: float, x2: float) -> None:
    if x2 < x1:
        x1, x2 = x2, x1
    start = max(0, math.floor(x1))
    end = min(width - 1, math.floor(x2))
    for x in range(start, end + 1):
        left = max(x1, x)
        right = min(x2, x + 1)
        coverage = _clamp(right - left)
        if coverage > 0:
            index = y * width + x
            mask[index] = max(mask[index], round(coverage * 255))


def rasterize_stroke(
    path: Path,
    width: int,
    height: int,
    *,
    width: float,
    line_cap: int = 0,
    line_join: int = 0,
) -> bytearray:
    mask = bytearray(width * height)
    radius = width * 0.5
    for subpath in path.subpaths:
        points = subpath.points
        if len(points) < 2:
            continue
        segment_count = len(points) if subpath.closed else len(points) - 1
        for index in range(segment_count):
            start = points[index]
            end = points[(index + 1) % len(points)]
            _stroke_segment(mask, width=width_px(width), canvas_width=width, canvas_height=height, start=start, end=end, radius=radius, cap=line_cap if not subpath.closed else 0)
        # Round joins are explicitly rasterized; bevel/miter currently share
        # the segment overlap, which is conservative for flattening.
        if line_join == 1:
            join_points = points if subpath.closed else points[1:-1]
            for point in join_points:
                _disc(mask, width, height, point[0], point[1], radius)
        if not subpath.closed and line_cap == 1:
            _disc(mask, width, height, points[0][0], points[0][1], radius)
            _disc(mask, width, height, points[-1][0], points[-1][1], radius)
    return mask


def width_px(value: float) -> float:
    return max(0.01, float(value))


def _stroke_segment(mask: bytearray, *, width: float, canvas_width: int, canvas_height: int, start, end, radius: float, cap: int) -> None:
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 == 0:
        _disc(mask, canvas_width, canvas_height, x1, y1, radius)
        return
    length = math.sqrt(length2)
    if cap == 2:  # square
        ux, uy = dx / length, dy / length
        x1 -= ux * radius
        y1 -= uy * radius
        x2 += ux * radius
        y2 += uy * radius
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
    min_x = max(0, math.floor(min(x1, x2) - radius - 1))
    max_x = min(canvas_width - 1, math.ceil(max(x1, x2) + radius + 1))
    min_y = max(0, math.floor(min(y1, y2) - radius - 1))
    max_y = min(canvas_height - 1, math.ceil(max(y1, y2) + radius + 1))
    for y in range(min_y, max_y + 1):
        py = y + 0.5
        for x in range(min_x, max_x + 1):
            px = x + 0.5
            t = ((px - x1) * dx + (py - y1) * dy) / length2
            if cap == 0:
                t = min(1.0, max(0.0, t))
            else:
                t = min(1.0, max(0.0, t))
            qx = x1 + t * dx
            qy = y1 + t * dy
            distance = math.hypot(px - qx, py - qy)
            coverage = _clamp(radius + 0.5 - distance)
            if coverage:
                index = y * canvas_width + x
                mask[index] = max(mask[index], round(coverage * 255))


def _disc(mask: bytearray, width: int, height: int, cx: float, cy: float, radius: float) -> None:
    min_x = max(0, math.floor(cx - radius - 1))
    max_x = min(width - 1, math.ceil(cx + radius + 1))
    min_y = max(0, math.floor(cy - radius - 1))
    max_y = min(height - 1, math.ceil(cy + radius + 1))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            distance = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            coverage = _clamp(radius + 0.5 - distance)
            if coverage:
                index = y * width + x
                mask[index] = max(mask[index], round(coverage * 255))
