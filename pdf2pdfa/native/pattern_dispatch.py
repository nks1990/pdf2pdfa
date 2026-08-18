"""Canonical PatternType 2 bridge to the owned shading dispatcher.

``PatternShadingRendererMixin`` owns Pattern color-space state and q/Q
semantics. This focused mixin replaces only the shading-pattern paint call so
PatternType 2 and direct ``sh`` operators share the exact same ShadingType
support matrix.
"""

from __future__ import annotations

from .page_render import RenderingError, UnsupportedRenderingError
from .pattern_render import (
    PatternRenderError,
    UnsupportedPatternError,
    _pattern_dictionary,
    _pattern_extgstate,
    _pattern_matrix,
)
from .raster import rasterize_fill
from .shading import ShadingError, UnsupportedShadingError
from .shading_dispatch import paint_owned_shading


class CanonicalPatternShadingMixin:
    def _paint_pattern_fill(self, *, even_odd: bool) -> None:
        selection = self._fill_pattern  # type: ignore[attr-defined]
        if selection is None or selection.pattern_type != 2:
            return super()._paint_pattern_fill(even_odd=even_odd)  # type: ignore[misc]
        if self._fill_pattern_space.has_base_space:  # type: ignore[attr-defined]
            raise UnsupportedPatternError(
                "PatternType 2 cannot be painted through an uncolored Pattern base color space"
            )

        dictionary = _pattern_dictionary(
            self.doc, selection.value, f"Pattern /{selection.name}"  # type: ignore[attr-defined]
        )
        shading = dictionary.get("Shading")
        if shading is None:
            raise PatternRenderError(f"shading pattern /{selection.name} has no /Shading")

        surface, state, _ = self._require()  # type: ignore[attr-defined]
        mask = rasterize_fill(
            self.path,  # type: ignore[attr-defined]
            surface.width,
            surface.height,
            even_odd=even_odd,
        )
        old_clip = bytearray(surface.clip)
        surface.clip = bytearray(
            (old_clip[index] * mask[index] + 127) // 255
            for index in range(len(old_clip))
        )
        try:
            matrix = _pattern_matrix(self.doc, dictionary)  # type: ignore[attr-defined]
            alpha, blend_mode = _pattern_extgstate(
                self.doc,  # type: ignore[attr-defined]
                dictionary,
                alpha=state.fill_alpha,
                blend_mode=state.blend_mode,
            )
            try:
                paint_owned_shading(
                    self.doc,  # type: ignore[attr-defined]
                    shading,
                    resources=self.resources,  # type: ignore[attr-defined]
                    surface=surface,
                    ctm=state.ctm.concat(matrix),
                    fill_alpha=alpha,
                    blend_mode=blend_mode,
                    soft_mask=getattr(self, "soft_mask", None),
                )
            except UnsupportedShadingError as exc:
                raise UnsupportedRenderingError(str(exc)) from exc
            except ShadingError as exc:
                raise RenderingError(str(exc)) from exc
        finally:
            surface.clip = old_clip
