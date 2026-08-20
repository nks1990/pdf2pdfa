"""Owned PDF transparency state that matters to knockout shape semantics."""

from __future__ import annotations

from .objects import PDFDict
from .page_render import RenderingError
from .structure import resolve


class TransparencySemanticsMixin:
    """Track /AIS and /TK through q/Q and nested group execution.

    The legacy renderer could ignore these flags while knockout was unsupported:
    ordinary source-over output does not expose the distinction between shape
    and opacity.  Knockout does, so they are now explicit renderer state.
    """

    def _ensure_transparency_semantics(self) -> None:
        if not hasattr(self, "_alpha_is_shape"):
            self._alpha_is_shape = False
        if not hasattr(self, "_text_knockout"):
            self._text_knockout = True
        if not hasattr(self, "_transparency_semantics_stack"):
            self._transparency_semantics_stack = []

    @property
    def alpha_is_shape(self) -> bool:
        self._ensure_transparency_semantics()
        return bool(self._alpha_is_shape)

    @property
    def text_knockout(self) -> bool:
        self._ensure_transparency_semantics()
        return bool(self._text_knockout)

    def render_page(self, page):
        self._alpha_is_shape = False
        self._text_knockout = True
        self._transparency_semantics_stack = []
        return super().render_page(page)

    def _instruction(self, instruction):
        self._ensure_transparency_semantics()
        if instruction.operator == "q":
            self._transparency_semantics_stack.append(
                (self._alpha_is_shape, self._text_knockout)
            )
            return super()._instruction(instruction)
        if instruction.operator == "Q":
            result = super()._instruction(instruction)
            if not self._transparency_semantics_stack:
                raise RenderingError("transparency semantics graphics stack underflow")
            self._alpha_is_shape, self._text_knockout = (
                self._transparency_semantics_stack.pop()
            )
            return result
        return super()._instruction(instruction)

    @staticmethod
    def _strict_bool(value, label: str) -> bool:
        if not isinstance(value, bool):
            raise RenderingError(f"{label} expects a boolean")
        return value

    def _extgstate(self, name: str) -> None:
        # Let the existing ExtGState engine apply alpha, blend, soft-mask,
        # stroke/dash and all other supported keys first.  It deliberately
        # treats AIS/TK as no-ops; this mixin owns their semantics.
        super()._extgstate(name)
        self._ensure_transparency_semantics()
        value = self._resolve_extgstate(name)
        if not isinstance(value, PDFDict):
            raise RenderingError("ExtGState resource is not a dictionary")
        if value.get("AIS") is not None:
            raw = resolve(self.doc, value.get("AIS"))
            self._alpha_is_shape = self._strict_bool(raw, "ExtGState/AIS")
        if value.get("TK") is not None:
            raw = resolve(self.doc, value.get("TK"))
            self._text_knockout = self._strict_bool(raw, "ExtGState/TK")

    def _snapshot_group_extras(self) -> dict[str, object]:
        self._ensure_transparency_semantics()
        result = super()._snapshot_group_extras()
        result["_alpha_is_shape"] = self._alpha_is_shape
        result["_text_knockout"] = self._text_knockout
        result["_transparency_semantics_stack"] = list(
            self._transparency_semantics_stack
        )
        return result

    def _restore_group_extras(self, values: dict[str, object]) -> None:
        super()._restore_group_extras(values)
        self._ensure_transparency_semantics()
