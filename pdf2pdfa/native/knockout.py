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
            offset = destination._offset(dx, dy)
            destination.pixels[offset + 0] = round(result.r * 255)
            destination.pixels[offset + 1] = round(result.g * 255)
            destination.pixels[offset + 2] = round(result.b * 255)
            destination.pixels[offset + 3] = round(result.a * 255)


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

    def clear(self) -> None:
        self.samples[:] = b"\x00" * len(self.samples)
