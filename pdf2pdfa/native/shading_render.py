"""Renderer mixin for owned axial/radial PDF shadings."""

from __future__ import annotations

from .content import ContentInstruction
from .page_render import RenderingError, UnsupportedRenderingError, _name, _resolve_resource
from .shading import ShadingError, UnsupportedShadingError, paint_shading


class ShadingRendererMixin:
    """Intercept ``sh`` and paint through the owned shading evaluator.

    The mixin is intentionally orthogonal to transparency. When combined with
    ``TransparencyRenderer`` it consumes that renderer's current ``soft_mask``
    and ordinary graphics-state alpha/blend mode, so shadings participate in
    the same owned compositing semantics as paths, text and images.
    """

    def _instruction(self, instruction: ContentInstruction) -> None:
        if instruction.operator != "sh":
            return super()._instruction(instruction)  # type: ignore[misc]

        args = list(instruction.operands)
        if len(args) != 1:
            raise RenderingError("sh expects exactly one shading resource name")
        name = _name(args[0], "sh")
        shading = _resolve_resource(self.doc, self.resources, "Shading", name)  # type: ignore[attr-defined]
        surface, state, _ = self._require()  # type: ignore[attr-defined]
        soft_mask = getattr(self, "soft_mask", None)
        try:
            paint_shading(
                self.doc,  # type: ignore[attr-defined]
                shading,
                resources=self.resources,  # type: ignore[attr-defined]
                surface=surface,
                ctm=state.ctm,
                fill_alpha=state.fill_alpha,
                blend_mode=state.blend_mode,
                soft_mask=soft_mask,
            )
        except UnsupportedShadingError as exc:
            raise UnsupportedRenderingError(str(exc)) from exc
        except ShadingError as exc:
            raise RenderingError(str(exc)) from exc
