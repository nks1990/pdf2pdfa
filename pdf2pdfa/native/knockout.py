"""Owned primitives for PDF knockout transparency compositing.

PDF knockout needs *shape* to remain distinct from opacity.  The source alpha
says how opaque an object is; the shape says where the object exists for
knockout replacement.  A half-opaque object can therefore have a full shape.

This module deliberately contains no PDF parser knowledge.  It provides the
small raster algebra used by the transparency renderer and can be tested in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .raster import Color, Surface


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def union_coverage(backdrop: float, source: float) -> float:
    """Return the union of two independent 0..1 coverage values."""

    backdrop = _clamp(backdrop)
    source = _clamp(source)
    return backdrop + source - backdrop * source


def knockout_pixel(backdrop: Color, source: Color, shape: float) -> Color:
    """Replace ``backdrop`` by ``source`` according to knockout ``shape``.

    This is the straight-alpha equivalent of the PDF knockout blend used by
    mature rasterizers: shape interpolates both the source/backdrop color and
    their alpha.  Crucially, ``shape`` is *not* source opacity.
    """

    h = _clamp(shape)
    b = backdrop.clamped()
    s = source.clamped()
    inv = 1.0 - h
    return Color(
        inv * b.r + h * s.r,
        inv * b.g + h * s.g,
        inv * b.b + h * s.b,
        inv * b.a + h * s.a,
    ).clamped()


def _write_pixel(surface: Surface, x: int, y: int, color: Color) -> None:
    color = color.clamped()
    offset = surface._offset(x, y)
    surface.pixels[offset + 0] = round(color.r * 255)
    surface.pixels[offset + 1] = round(color.g * 255)
    surface.pixels[offset + 2] = round(color.b * 255)
    surface.pixels[offset + 3] = round(color.a * 255)


def knockout_surface(
    destination: Surface,
    source: Surface,
    shape: bytes | bytearray,
    *,
    x: int = 0,
    y: int = 0,
) -> None:
    """Apply one knockout object surface to ``destination``.

    ``shape`` is one byte per source pixel.  The destination clip is respected
    as an additional geometric boundary; it does not alter the source opacity.
    """

    if len(shape) != source.width * source.height:
        raise ValueError("knockout shape dimensions do not match source surface")
    for sy in range(source.height):
        dy = y + sy
        if dy < 0 or dy >= destination.height:
            continue
        for sx in range(source.width):
            dx = x + sx
            if dx < 0 or dx >= destination.width:
                continue
            source_index = sy * source.width + sx
            h = shape[source_index] / 255.0
            if h <= 0.0:
                continue
            clip = destination.clip[dy * destination.width + dx] / 255.0
            h *= clip
            if h <= 0.0:
                continue
            result = knockout_pixel(
                destination.get_pixel(dx, dy),
                source.get_pixel(sx, sy),
                h,
            )
            _write_pixel(destination, dx, dy, result)


@dataclass(slots=True)
class ShapeAccumulator:
    """One-byte shape plane with PDF-style union accumulation."""

    width: int
    height: int
    samples: bytearray

    @classmethod
    def empty(cls, width: int, height: int) -> "ShapeAccumulator":
        if width <= 0 or height <= 0:
            raise ValueError("shape dimensions must be positive")
        if width * height > 250_000_000:
            raise ValueError("shape plane is too large")
        return cls(width, height, bytearray(width * height))

    def add(self, mask: bytes | bytearray, *, scale: float = 1.0) -> None:
        if len(mask) != self.width * self.height:
            raise ValueError("shape mask dimensions do not match accumulator")
        scale = _clamp(scale)
        for index, raw in enumerate(mask):
            incoming = (raw / 255.0) * scale
            if incoming <= 0.0:
                continue
            current = self.samples[index] / 255.0
            self.samples[index] = round(union_coverage(current, incoming) * 255)

    def add_pixel(self, index: int, coverage: float) -> None:
        incoming = _clamp(coverage)
        if incoming <= 0.0:
            return
        current = self.samples[index] / 255.0
        self.samples[index] = round(union_coverage(current, incoming) * 255)

    def clear(self) -> None:
        self.samples[:] = b"\x00" * len(self.samples)


class KnockoutSurface(Surface):
    """Surface that composites every paint sample against a fixed group backdrop.

    The current pixels hold the evolving knockout-group result.  A second,
    immutable pixel plane stores the group backdrop.  Each paint sample is
    first rendered against that fixed backdrop and then replaces the evolving
    result according to its independent shape coverage.

    ``shape_clip`` is used only when alpha is opacity (AIS=false).  It lets the
    renderer provide the geometric clip that existed *before* an opacity soft
    mask was folded into ``clip``.
    """

    def __init__(self, backdrop: Surface):
        super().__init__(
            backdrop.width,
            backdrop.height,
            background=Color(0, 0, 0, 0),
        )
        self.pixels[:] = backdrop.pixels
        self.clip[:] = backdrop.clip
        self._knockout_backdrop = bytes(backdrop.pixels)
        self.shape = ShapeAccumulator.empty(self.width, self.height)
        self.shape_clip: bytearray | None = None
        self.alpha_is_shape = False

    def _fixed_backdrop_pixel(self, x: int, y: int) -> Color:
        offset = self._offset(x, y)
        data = self._knockout_backdrop
        return Color(
            data[offset + 0] / 255.0,
            data[offset + 1] / 255.0,
            data[offset + 2] / 255.0,
            data[offset + 3] / 255.0,
        )

    def composite_pixel(
        self,
        x: int,
        y: int,
        source: Color,
        *,
        coverage: float = 1.0,
        blend_mode: str = "Normal",
    ) -> None:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return
        index = y * self.width + x
        geometric = _clamp(coverage)
        if geometric <= 0.0:
            return

        paint_clip = self.clip[index] / 255.0
        if paint_clip <= 0.0:
            return

        original = source.clamped()
        if self.alpha_is_shape:
            # AIS=true: alpha constant + soft mask are shape, not opacity.
            shape = geometric * paint_clip * original.a
            paint_source = Color(original.r, original.g, original.b, 1.0)
            paint_coverage = geometric * original.a
        else:
            # AIS=false: source alpha and soft mask are opacity.  Knockout shape
            # remains geometric, so use the pre-soft-mask clip when supplied.
            shape_clip = (
                self.shape_clip[index] / 255.0
                if self.shape_clip is not None
                else paint_clip
            )
            shape = geometric * shape_clip
            paint_source = original
            paint_coverage = geometric

        shape = _clamp(shape)
        if shape <= 0.0:
            return

        current = self.get_pixel(x, y)
        fixed = self._fixed_backdrop_pixel(x, y)
        _write_pixel(self, x, y, fixed)
        try:
            super().composite_pixel(
                x,
                y,
                paint_source,
                coverage=paint_coverage,
                blend_mode=blend_mode,
            )
            object_result = self.get_pixel(x, y)
        finally:
            _write_pixel(self, x, y, current)

        _write_pixel(self, x, y, knockout_pixel(current, object_result, shape))
        self.shape.add_pixel(index, shape)
