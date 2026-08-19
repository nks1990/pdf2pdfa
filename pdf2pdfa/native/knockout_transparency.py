"""Owned knockout transparency-group compositor.

The implementation intentionally starts with the subset for which shape can be
proved correct with the current raster primitives. Unsupported interactions
(AIS=true, soft masks *inside* a knockout group, TK=false text, Type3 glyphs,
patterns, masked images, compound fill+stroke objects, Form transactions and
nested knockout execution) fail closed instead of degrading to ordinary alpha
compositing.
"""

from __future__ import annotations

from .knockout import KnockoutSurface
from .nonisolated_transparency import _supported_group_color_space
from .objects import PDFDict, PDFName, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError, _resolve_resource
from .raster import Color, Surface
from .structure import resolve


_ALPHA_TOLERANCE = 3.0 / 255.0
_PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
_COMPOUND_PATH_PAINT = {"B", "B*", "b", "b*"}
_TEXT_PAINT = {"Tj", "TJ", "'", '"'}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class KnockoutTransparencyRendererMixin:
    def _in_knockout_execution(self) -> bool:
        surface = getattr(self, "surface", None)
        return isinstance(surface, KnockoutSurface)

    @staticmethod
    def _pattern_active(value) -> bool:
        return bool(getattr(value, "active", False))

    def _guard_knockout_paint(self, operator: str) -> None:
        if not self._in_knockout_execution():
            return
        if self.alpha_is_shape:  # type: ignore[attr-defined]
            raise UnsupportedRenderingError(
                "knockout transparency with /AIS true requires shape-alpha object transactions"
            )
        if operator in _COMPOUND_PATH_PAINT:
            raise UnsupportedRenderingError(
                "combined fill+stroke path inside a knockout group requires one compound-object transaction"
            )
        if operator in _TEXT_PAINT:
            if not self.text_knockout:  # type: ignore[attr-defined]
                raise UnsupportedRenderingError(
                    "knockout transparency with /TK false requires whole-text-object shape transactions"
                )
            text = getattr(self, "text", None)
            text_state = getattr(text, "state", None)
            font = getattr(text_state, "font", None)
            if bool(getattr(font, "is_type3", False)):
                raise UnsupportedRenderingError(
                    "Type3 text inside a knockout group requires glyph-level shape transactions"
                )
            if getattr(text_state, "render_mode", 0) in {2, 6}:
                raise UnsupportedRenderingError(
                    "fill+stroke text inside a knockout group requires one glyph-object transaction"
                )
            if self._pattern_active(getattr(self, "_fill_pattern_space", None)) or self._pattern_active(
                getattr(self, "_stroke_pattern_space", None)
            ):
                raise UnsupportedRenderingError(
                    "pattern-colored text inside a knockout group requires text-object pattern shape transactions"
                )
        if operator in _PATH_PAINT:
            fill = operator in {"f", "F", "f*", "B", "B*", "b", "b*"}
            stroke = operator in {"S", "s", "B", "B*", "b", "b*"}
            if fill and self._pattern_active(getattr(self, "_fill_pattern_space", None)):
                raise UnsupportedRenderingError(
                    "pattern fills inside a knockout group require pattern-object shape transactions"
                )
            if stroke and self._pattern_active(getattr(self, "_stroke_pattern_space", None)):
                raise UnsupportedRenderingError(
                    "pattern strokes inside a knockout group require pattern-object shape transactions"
                )

    def _instruction(self, instruction):
        op = instruction.operator
        if op in _PATH_PAINT or op in _TEXT_PAINT or op in {"Do", "sh"}:
            self._guard_knockout_paint(op)
        return super()._instruction(instruction)

    def _inline_image(self, image) -> None:
        self._guard_knockout_paint("inline-image")
        return super()._inline_image(image)

    def _xobject(self, name: str) -> None:
        if self._in_knockout_execution():
            value = _resolve_resource(self.doc, self.resources, "XObject", name)
            if not isinstance(value, PDFStream):
                raise RenderingError("XObject is not a stream")
            subtype = resolve(self.doc, value.get("Subtype"))
            if isinstance(subtype, PDFName) and subtype.value == "Form":
                raise UnsupportedRenderingError(
                    "Form XObject inside a knockout group requires one Form-object shape transaction"
                )
        return super()._xobject(name)

    def _draw_image(self, image, matrix) -> None:
        if self._in_knockout_execution():
            # After image decoding, alpha alone no longer tells us whether the
            # source came from an opacity SMask or from a shape/stencil mask.
            # Until the decoder carries that provenance, only fully opaque
            # images have unambiguous knockout shape semantics.
            if any(alpha != 255 for alpha in image.rgba[3::4]):
                raise UnsupportedRenderingError(
                    "masked/transparent images inside a knockout group require image-alpha provenance"
                )
        return super()._draw_image(image, matrix)

    def _extgstate(self, name: str) -> None:
        if self._in_knockout_execution():
            value = self._resolve_extgstate(name)
            raw_smask = resolve(self.doc, value.get("SMask")) if value.get("SMask") is not None else None
            if raw_smask is not None and not (
                isinstance(raw_smask, PDFName) and raw_smask.value == "None"
            ):
                raise UnsupportedRenderingError(
                    "soft masks inside a knockout group require separate pre-mask shape coverage"
                )
            raw_ais = resolve(self.doc, value.get("AIS")) if value.get("AIS") is not None else None
            if raw_ais is True:
                raise UnsupportedRenderingError(
                    "knockout transparency with /AIS true requires shape-alpha object transactions"
                )
        return super()._extgstate(name)

    def _paint_transparency_group(self, form: PDFStream, group: PDFDict) -> None:
        parent_surface, state, _ = self._require()
        isolated, knockout = self._group_flags(group)
        if not knockout:
            return super()._paint_transparency_group(form, group)
        if self._in_knockout_execution():
            raise UnsupportedRenderingError(
                "nested transparency groups inside a knockout group require nested object-shape transactions"
            )
        _supported_group_color_space(self.doc, group)
        if self.alpha_is_shape:  # type: ignore[attr-defined]
            raise UnsupportedRenderingError(
                "knockout group boundary with /AIS true requires shape-alpha object transactions"
            )

        caller_alpha = state.fill_alpha
        caller_blend = state.blend_mode
        caller_mask = bytearray(self.soft_mask) if self.soft_mask is not None else None

        if isolated:
            transparent = Surface(
                parent_surface.width,
                parent_surface.height,
                background=Color(0, 0, 0, 0),
            )
            transparent.clip[:] = parent_surface.clip
            result = self._execute_group_on_surface(form, KnockoutSurface(transparent))
            self._composite_knockout_group_boundary(
                result,
                caller_alpha=caller_alpha,
                caller_blend=caller_blend,
                caller_mask=caller_mask,
            )
            return

        before = parent_surface.copy()
        result = self._execute_group_on_surface(form, KnockoutSurface(before.copy()))

        transparent = Surface(
            parent_surface.width,
            parent_surface.height,
            background=Color(0, 0, 0, 0),
        )
        transparent.clip[:] = parent_surface.clip
        alpha_run = self._execute_group_on_surface(form, KnockoutSurface(transparent))

        for y in range(parent_surface.height):
            row = y * parent_surface.width
            for x in range(parent_surface.width):
                index = row + x
                alpha_s = alpha_run.get_pixel(x, y).a
                if alpha_s <= 0.0:
                    continue
                backdrop = before.get_pixel(x, y)
                composited = result.get_pixel(x, y)
                expected_alpha = alpha_s + backdrop.a * (1.0 - alpha_s)
                if abs(composited.a - expected_alpha) > _ALPHA_TOLERANCE:
                    raise RenderingError(
                        "non-isolated knockout group alpha reconstruction is inconsistent"
                    )

                source_rgb: list[float] = []
                for cb, cr in zip(
                    (backdrop.r, backdrop.g, backdrop.b),
                    (composited.r, composited.g, composited.b),
                ):
                    premul_result = composited.a * cr
                    premul_backdrop = backdrop.a * cb
                    value = (
                        premul_result - (1.0 - alpha_s) * premul_backdrop
                    ) / alpha_s
                    source_rgb.append(_clamp(value))

                alpha = alpha_s * caller_alpha
                if caller_mask is not None:
                    alpha *= caller_mask[index] / 255.0
                if alpha <= 0.0:
                    continue
                parent_surface.composite_pixel(
                    x,
                    y,
                    Color(source_rgb[0], source_rgb[1], source_rgb[2], alpha),
                    blend_mode=caller_blend,
                )

    def _composite_knockout_group_boundary(
        self,
        group_surface: Surface,
        *,
        caller_alpha: float,
        caller_blend: str,
        caller_mask: bytearray | None,
    ) -> None:
        surface, _, _ = self._require()
        for y in range(surface.height):
            row = y * surface.width
            for x in range(surface.width):
                index = row + x
                source = group_surface.get_pixel(x, y)
                if source.a <= 0.0:
                    continue
                alpha = source.a * caller_alpha
                if caller_mask is not None:
                    alpha *= caller_mask[index] / 255.0
                if alpha <= 0.0:
                    continue
                surface.composite_pixel(
                    x,
                    y,
                    Color(source.r, source.g, source.b, alpha),
                    blend_mode=caller_blend,
                )
