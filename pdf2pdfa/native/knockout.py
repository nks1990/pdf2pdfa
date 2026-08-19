"""Owned primitives for PDF knockout transparency compositing.

Knockout needs three independent quantities at each point:

* the immutable group backdrop;
* accumulated group shape;
* accumulated group alpha.

Object shape is deliberately not folded into object opacity.  This module uses
the PDF group-compositing recurrence directly, so fractional scan-conversion
coverage is applied once as shape instead of being multiplied into alpha twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from .raster import Color, Surface, blend_rgb


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def union_coverage(backdrop: float, source: float) -> float:
    """Return the union of two independent 0..1 coverage/alpha values."""

    backdrop = _clamp(backdrop)
    source = _clamp(source)
    return backdrop + source - backdrop * source


def composite_knockout_element(
    backdrop: Color,
    previous: Color,
    group_alpha: float,
    source: Color,
    shape: float,
    *,
    blend_mode: str = "Normal",
) -> tuple[Color, float]:
    """Composite one intrinsic elementary object in a knockout group.

    ``source.a`` is source *opacity* (q_s), not source alpha. ``shape`` is the
    independent source shape (f_s), including geometry/scan conversion/clip for
    the currently supported AIS=false subset.  ``group_alpha`` is alpha_g from
    the previous group element, which cannot in general be recovered from the
    visible surface alpha when the group backdrop is opaque.
    """

    cb = backdrop.clamped()
    ci = previous.clamped()
    cs = source.clamped()
    fs = _clamp(shape)
    qs = cs.a
    alpha_s = fs * qs
    alpha_b = cb.a
    alpha_g_prev = _clamp(group_alpha)

    # PDF group alpha recurrence for a knockout group (b = 0 / fixed group
    # backdrop for every element).
    alpha_g = (
        (1.0 - fs) * alpha_g_prev
        + (fs - alpha_s) * alpha_b
        + alpha_s
    )
    alpha_g = _clamp(alpha_g)
    alpha_r = union_coverage(alpha_b, alpha_g)

    blended = blend_rgb(
        (cb.r, cb.g, cb.b),
        (cs.r, cs.g, cs.b),
        blend_mode,
    )
    channels: list[float] = []
    for previous_c, backdrop_c, source_c, blend_c in zip(
        (ci.r, ci.g, ci.b),
        (cb.r, cb.g, cb.b),
        (cs.r, cs.g, cs.b),
        blended,
    ):
        ct = (
            (fs - alpha_s) * alpha_b * backdrop_c
            + alpha_s
            * (
                (1.0 - alpha_b) * source_c
                + alpha_b * blend_c
            )
        )
        numerator = (1.0 - fs) * ci.a * previous_c + ct
        channels.append(_clamp(numerator / alpha_r) if alpha_r > 0.0 else 0.0)

    return Color(channels[0], channels[1], channels[2], alpha_r), alpha_g


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
    """Composite a one-element knockout source surface over destination.

    This convenience primitive is mainly useful for focused raster tests. Each
    source pixel supplies intrinsic color/opacity and ``shape`` supplies the
    independent object shape. The destination pixel is both group backdrop and
    previous result because there is only one element.
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
            fs = (shape[source_index] / 255.0) * (
                destination.clip[dy * destination.width + dx] / 255.0
            )
            if fs <= 0.0:
                continue
            backdrop = destination.get_pixel(dx, dy)
            result, _ = composite_knockout_element(
                backdrop,
                backdrop,
                0.0,
                source.get_pixel(sx, sy),
                fs,
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
    """Surface implementing the PDF knockout recurrence for intrinsic objects.

    The visible pixels hold C_i/alpha_i.  The immutable backdrop stores C_0 and
    alpha_0.  ``group_alpha`` stores alpha_g independently, which is essential
    for non-isolated groups with opaque backdrops.  ``shape`` stores f_g for
    diagnostics/future group interactions.

    The production knockout path currently admits AIS=false only, so the
    incoming ``coverage`` is source shape and ``source.a`` is source opacity.
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
        self.group_alpha = bytearray(self.width * self.height)

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
        fs = _clamp(coverage) * (self.clip[index] / 255.0)
        if fs <= 0.0:
            return

        result, alpha_g = composite_knockout_element(
            self._fixed_backdrop_pixel(x, y),
            self.get_pixel(x, y),
            self.group_alpha[index] / 255.0,
            source,
            fs,
            blend_mode=blend_mode,
        )
        _write_pixel(self, x, y, result)
        self.group_alpha[index] = round(alpha_g * 255)
        self.shape.add_pixel(index, fs)
