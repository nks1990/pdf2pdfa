"""Owned non-isolated transparency-group compositor.

A non-isolated group uses the current backdrop while its children are painted.
Simply rendering on a copy of the backdrop and then source-over compositing that
copy would apply the backdrop twice.  This implementation renders the group:

1. over the actual caller backdrop to obtain the group result;
2. over transparent black to obtain the group's source alpha/shape.

For each pixel it reconstructs the effective straight-alpha source color whose
normal source-over composition recreates the backdrop run, then applies the
form boundary alpha/blend/soft-mask once to the real parent surface.

Knockout groups and explicit non-RGB group blending color spaces remain
fail-closed because they require additional shape/knockout/color-space state.
"""

from __future__ import annotations

import copy

from .cff_text_render import OwnedOutlineTextRenderer
from .objects import PDFDict, PDFName, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError
from .raster import Color, Path, Surface, rasterize_fill
from .structure import decoded_stream_bytes, resolve
from .text_render import TextState
from .transparency_render import _bbox, _matrix


_MAX_GROUP_DEPTH = 32
_ALPHA_TOLERANCE = 3.0 / 255.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _supported_group_color_space(doc, group: PDFDict) -> None:
    raw = resolve(doc, group.get("CS")) if group.get("CS") is not None else None
    if raw is None:
        return
    if isinstance(raw, PDFName) and raw.value in {"DeviceRGB", "RGB"}:
        return
    raise UnsupportedRenderingError(
        "explicit non-RGB transparency-group /CS requires owned group-color-space compositing"
    )


def _copy_text_state(source) -> TextState:
    state = TextState()
    state.font = source.font
    state.font_size = source.font_size
    state.char_spacing = source.char_spacing
    state.word_spacing = source.word_spacing
    state.horizontal_scale = source.horizontal_scale
    state.leading = source.leading
    state.rise = source.rise
    state.render_mode = source.render_mode
    return state


class NonIsolatedTransparencyRendererMixin:
    """Add non-isolated group support and harden the shared group executor."""

    _GROUP_EXTRA_FIELDS = (
        "_fill_pattern_space",
        "_stroke_pattern_space",
        "_fill_pattern",
        "_stroke_pattern",
        "_pattern_stack",
        "_tiling_pattern_depth",
        "_fill_uncolored_space",
        "_stroke_uncolored_space",
        "_fill_uncolored_selection",
        "_stroke_uncolored_selection",
        "_uncolored_stack",
        "_uncolored_pattern_depth",
    )

    def _snapshot_group_extras(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for name in self._GROUP_EXTRA_FIELDS:
            if not hasattr(self, name):
                continue
            value = getattr(self, name)
            result[name] = list(value) if isinstance(value, list) else value
        return result

    def _restore_group_extras(self, values: dict[str, object]) -> None:
        for name, value in values.items():
            current = getattr(self, name, None)
            if isinstance(current, list) and isinstance(value, list):
                current[:] = value
            else:
                setattr(self, name, value)

    def _execute_group_on_surface(self, form: PDFStream, group_surface: Surface) -> Surface:
        parent_surface, parent_state, parent_text = self._require()
        if self._group_depth >= _MAX_GROUP_DEPTH:
            raise RenderingError(
                f"transparency group nesting exceeds {_MAX_GROUP_DEPTH}"
            )
        form_matrix = _matrix(self.doc, form)
        bbox = _bbox(self.doc, form)

        inner_state = parent_state.clone()
        inner_state.ctm = parent_state.ctm.concat(form_matrix)
        # Form boundary alpha/blend/soft-mask is applied by the caller after the
        # group is reduced to one source object.
        inner_state.fill_alpha = 1.0
        inner_state.stroke_alpha = 1.0
        inner_state.blend_mode = "Normal"
        inner_state.clip[:] = group_surface.clip

        clip_path = Path()
        corners = [
            inner_state.ctm.transform(bbox[0], bbox[1]),
            inner_state.ctm.transform(bbox[2], bbox[1]),
            inner_state.ctm.transform(bbox[2], bbox[3]),
            inner_state.ctm.transform(bbox[0], bbox[3]),
        ]
        clip_path.move_to(*corners[0])
        for point in corners[1:]:
            clip_path.line_to(*point)
        clip_path.close()
        group_surface.apply_clip_mask(
            rasterize_fill(clip_path, group_surface.width, group_surface.height)
        )
        inner_state.clip[:] = group_surface.clip

        raw_resources = (
            resolve(self.doc, form.get("Resources"))
            if form.get("Resources") is not None
            else self.resources
        )
        resources = raw_resources if isinstance(raw_resources, PDFDict) else self.resources

        saved_surface = self.surface
        saved_state = self.state
        saved_text = self.text
        saved_resources = self.resources
        saved_path = self.path
        saved_pending = self.pending_clip
        saved_path_ctm = self._path_ctm
        saved_mixed = self._mixed_path_ctm
        saved_soft = self.soft_mask
        saved_soft_stack = self._soft_stack
        saved_stack = self.stack
        extras = self._snapshot_group_extras()

        self._group_depth += 1
        try:
            self.surface = group_surface
            self.state = inner_state
            group_text = OwnedOutlineTextRenderer(
                group_surface,
                ctm=inner_state.ctm,
                type3_painter=self._paint_type3_glyph,
            )
            group_text.state = _copy_text_state(parent_text.state)
            group_text.in_text_object = False
            self.text = group_text
            self.resources = resources
            self.path = Path()
            self.pending_clip = None
            self._path_ctm = None
            self._mixed_path_ctm = False
            self.soft_mask = None
            self._soft_stack = []
            self.stack = []
            self._execute(
                decoded_stream_bytes(self.doc, form, label="transparency group Form"),
                resources,
            )
            if self.stack:
                raise RenderingError("transparency group ended with unbalanced q/Q")
            if self.text.in_text_object:
                raise RenderingError("transparency group ended inside BT/ET")
        finally:
            self._group_depth -= 1
            self.surface = saved_surface
            self.state = saved_state
            self.text = saved_text
            self.resources = saved_resources
            self.path = saved_path
            self.pending_clip = saved_pending
            self._path_ctm = saved_path_ctm
            self._mixed_path_ctm = saved_mixed
            self.soft_mask = saved_soft
            self._soft_stack = saved_soft_stack
            self.stack = saved_stack
            self._restore_group_extras(extras)
        return group_surface

    def _render_isolated_group(self, form: PDFStream) -> Surface:
        parent_surface, _, _ = self._require()
        surface = Surface(
            parent_surface.width,
            parent_surface.height,
            background=Color(0, 0, 0, 0),
        )
        surface.clip[:] = parent_surface.clip
        return self._execute_group_on_surface(form, surface)

    def _paint_transparency_group(self, form: PDFStream, group: PDFDict) -> None:
        isolated, knockout = self._group_flags(group)
        _supported_group_color_space(self.doc, group)
        if knockout:
            raise UnsupportedRenderingError(
                "knockout transparency groups require owned knockout shape compositor"
            )
        if isolated:
            return super()._paint_transparency_group(form, group)

        surface, state, _ = self._require()
        caller_alpha = state.fill_alpha
        caller_blend = state.blend_mode
        caller_mask = bytearray(self.soft_mask) if self.soft_mask is not None else None

        before = surface.copy()
        backdrop_result = self._execute_group_on_surface(form, before.copy())
        alpha_source = Surface(
            surface.width,
            surface.height,
            background=Color(0, 0, 0, 0),
        )
        alpha_source.clip[:] = surface.clip
        alpha_source = self._execute_group_on_surface(form, alpha_source)

        for y in range(surface.height):
            row = y * surface.width
            for x in range(surface.width):
                index = row + x
                alpha_s = alpha_source.get_pixel(x, y).a
                if alpha_s <= 0.0:
                    continue
                backdrop = before.get_pixel(x, y)
                result = backdrop_result.get_pixel(x, y)

                expected_alpha = alpha_s + backdrop.a * (1.0 - alpha_s)
                if abs(result.a - expected_alpha) > _ALPHA_TOLERANCE:
                    raise RenderingError(
                        "non-isolated group alpha reconstruction is inconsistent with source-over semantics"
                    )

                source_rgb: list[float] = []
                for channel, (cb, cr) in enumerate(
                    zip(
                        (backdrop.r, backdrop.g, backdrop.b),
                        (result.r, result.g, result.b),
                    )
                ):
                    premul_result = result.a * cr
                    premul_backdrop = backdrop.a * cb
                    value = (
                        premul_result
                        - (1.0 - alpha_s) * premul_backdrop
                    ) / alpha_s
                    source_rgb.append(_clamp(value))

                alpha = alpha_s * caller_alpha
                if caller_mask is not None:
                    alpha *= caller_mask[index] / 255.0
                if alpha <= 0.0:
                    continue
                surface.composite_pixel(
                    x,
                    y,
                    Color(source_rgb[0], source_rgb[1], source_rgb[2], alpha),
                    blend_mode=caller_blend,
                )
