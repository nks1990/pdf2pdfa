"""Owned Pattern stroking through the canonical pattern-fill painters.

PDF patterns are valid stroking colors as well as nonstroking colors. This
mixin does not implement a second shading/tiling engine. It first rasterizes
the exact affine PDF stroke geometry to an alpha coverage mask using the same
owned dash/cap/join/hairline machinery as ordinary strokes. That mask becomes
a temporary clip and the existing PatternType 1/2 fill dispatcher paints
through it.

While painting the stroke, stroking pattern/base-color state and ``CA`` are
projected temporarily onto the fill-side fields consumed by the existing
pattern painters. They are restored immediately afterwards, so q/Q and later
fills observe the original independent graphics state.
"""

from __future__ import annotations

import copy

from .affine_stroke import StrokeError, stroke_affine
from .page_render import RenderingError, UnsupportedRenderingError
from .raster import Color, Path, Surface


class PatternStrokeRendererMixin:
    """Make Pattern color spaces production-reachable for path stroking."""

    @staticmethod
    def _device_rect(width: int, height: int) -> Path:
        path = Path()
        path.move_to(0.0, 0.0)
        path.line_to(float(width), 0.0)
        path.line_to(float(width), float(height))
        path.line_to(0.0, float(height))
        path.close()
        return path

    def _pattern_stroke_mask(self) -> bytearray:
        surface, state, _ = self._require()  # type: ignore[attr-defined]
        path_ctm = getattr(self, "_path_ctm", None) or state.ctm
        if getattr(self, "_mixed_path_ctm", False) or path_ctm != state.ctm:
            raise UnsupportedRenderingError(
                "pattern stroke path constructed under multiple/different CTMs "
                "requires segmented affine stroke state"
            )

        mask_surface = Surface(
            surface.width,
            surface.height,
            background=Color(0, 0, 0, 0),
        )
        try:
            stroke_affine(
                mask_surface,
                self.path,  # type: ignore[attr-defined]
                path_ctm=path_ctm,
                line_width=state.line_width,
                line_cap=state.line_cap,
                line_join=state.line_join,
                miter_limit=state.miter_limit,
                dash_array=state.dash_array,
                dash_phase=state.dash_phase,
                color=Color(1, 1, 1, 1),
                blend_mode="Normal",
            )
        except StrokeError as exc:
            raise RenderingError(str(exc)) from exc
        return bytearray(mask_surface.pixels[3::4])

    def _paint_selected_pattern_stroke(self) -> None:
        surface, state, _ = self._require()  # type: ignore[attr-defined]
        stroke_pattern = getattr(self, "_stroke_pattern", None)
        if stroke_pattern is None:
            raise RenderingError(
                "Pattern stroke color space has no selected pattern (missing SCN)"
            )

        coverage = self._pattern_stroke_mask()
        old_surface_clip = bytearray(surface.clip)
        old_state_clip = bytearray(state.clip)
        combined = bytearray(
            (old_surface_clip[index] * coverage[index] + 127) // 255
            for index in range(len(old_surface_clip))
        )
        if not any(combined):
            return

        saved_path = copy.deepcopy(self.path)  # type: ignore[attr-defined]
        saved_fill_space = state.fill_space
        saved_fill_color = state.fill_color
        saved_fill_alpha = state.fill_alpha
        saved_fill_pattern_space = self._fill_pattern_space  # type: ignore[attr-defined]
        saved_fill_pattern = self._fill_pattern  # type: ignore[attr-defined]
        saved_fill_uncolored_space = getattr(self, "_fill_uncolored_space", None)
        saved_fill_uncolored_selection = getattr(self, "_fill_uncolored_selection", None)

        try:
            surface.clip[:] = combined
            state.clip[:] = combined
            self.path = self._device_rect(surface.width, surface.height)  # type: ignore[attr-defined]

            # Reuse the fill-side pattern compositor with stroking semantics.
            state.fill_space = state.stroke_space
            state.fill_color = state.stroke_color
            state.fill_alpha = state.stroke_alpha
            self._fill_pattern_space = self._stroke_pattern_space  # type: ignore[attr-defined]
            self._fill_pattern = stroke_pattern  # type: ignore[attr-defined]
            if hasattr(self, "_stroke_uncolored_space"):
                self._fill_uncolored_space = self._stroke_uncolored_space  # type: ignore[attr-defined]
                self._fill_uncolored_selection = self._stroke_uncolored_selection  # type: ignore[attr-defined]

            self._paint_pattern_fill(even_odd=False)  # type: ignore[attr-defined]
        finally:
            self.path = saved_path  # type: ignore[attr-defined]
            state.fill_space = saved_fill_space
            state.fill_color = saved_fill_color
            state.fill_alpha = saved_fill_alpha
            self._fill_pattern_space = saved_fill_pattern_space  # type: ignore[attr-defined]
            self._fill_pattern = saved_fill_pattern  # type: ignore[attr-defined]
            if hasattr(self, "_fill_uncolored_space"):
                self._fill_uncolored_space = saved_fill_uncolored_space  # type: ignore[attr-defined]
                self._fill_uncolored_selection = saved_fill_uncolored_selection  # type: ignore[attr-defined]
            surface.clip[:] = old_surface_clip
            state.clip[:] = old_state_clip

    def _paint_operator(self, op: str, args: list[object]) -> None:
        stroke = op in {"S", "s", "B", "B*", "b", "b*"}
        stroke_pattern_active = bool(
            stroke
            and getattr(self, "_stroke_pattern_space", None) is not None
            and self._stroke_pattern_space.active  # type: ignore[attr-defined]
        )
        if not stroke_pattern_active:
            return super()._paint_operator(op, args)  # type: ignore[misc]
        if args:
            raise RenderingError(f"{op} takes no operands")

        surface, state, _ = self._require()  # type: ignore[attr-defined]
        close = op in {"s", "b", "b*"}
        if close and self.path.subpaths:  # type: ignore[attr-defined]
            self.path.close()  # type: ignore[attr-defined]
        fill = op in {"B", "B*", "b", "b*"}
        even_odd = op in {"B*", "b*"}

        # W/W* applies to the same terminated path before either fill or stroke.
        self._apply_pending_clip()  # type: ignore[attr-defined]

        # PatternType 1 fill painters are allowed to consume/reset the current
        # path as part of their own completion semantics. Compound B/b must
        # nevertheless stroke the exact same path object afterwards, so keep a
        # geometry/affine snapshot across the fill phase.
        compound_path = copy.deepcopy(self.path)  # type: ignore[attr-defined]
        compound_path_ctm = getattr(self, "_path_ctm", None)
        compound_mixed_path_ctm = getattr(self, "_mixed_path_ctm", False)

        if fill:
            if self._fill_pattern_space.active:  # type: ignore[attr-defined]
                self._paint_pattern_fill(even_odd=even_odd)  # type: ignore[attr-defined]
            else:
                surface.fill_path(
                    self.path,  # type: ignore[attr-defined]
                    Color(
                        state.fill_color.r,
                        state.fill_color.g,
                        state.fill_color.b,
                        state.fill_alpha,
                    ),
                    even_odd=even_odd,
                    blend_mode=state.blend_mode,
                )

        self.path = compound_path  # type: ignore[attr-defined]
        if hasattr(self, "_path_ctm"):
            self._path_ctm = compound_path_ctm  # type: ignore[attr-defined]
            self._mixed_path_ctm = compound_mixed_path_ctm  # type: ignore[attr-defined]
        self._paint_selected_pattern_stroke()

        self.path.clear()  # type: ignore[attr-defined]
        self.pending_clip = None  # type: ignore[attr-defined]
        if hasattr(self, "_path_ctm"):
            self._path_ctm = None  # type: ignore[attr-defined]
            self._mixed_path_ctm = False  # type: ignore[attr-defined]
