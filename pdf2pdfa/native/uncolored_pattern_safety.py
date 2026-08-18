"""Fail-closed guards for PaintType 2 cell features not yet shape-proven."""

from __future__ import annotations

from .objects import PDFDict, PDFName
from .page_render import RenderingError, _name, _resolve_resource
from .pattern_render import UnsupportedPatternError
from .structure import resolve


class UncoloredPatternSafetyMixin:
    def _instruction(self, instruction) -> None:
        if getattr(self, "_uncolored_pattern_depth", 0) and instruction.operator == "gs":
            args = list(instruction.operands)
            if len(args) != 1:
                raise RenderingError("gs expects one ExtGState name")
            state_value = _resolve_resource(
                self.doc,
                self.resources,
                "ExtGState",
                _name(args[0], "gs"),
            )
            state = resolve(self.doc, state_value)
            if not isinstance(state, PDFDict):
                raise RenderingError("ExtGState resource is not a dictionary")
            if state.get("SMask") is not None:
                smask = resolve(self.doc, state.get("SMask"))
                if not (isinstance(smask, PDFName) and smask.value == "None"):
                    raise UnsupportedPatternError(
                        "PaintType 2 cell soft masks require owned shape-only soft-mask semantics"
                    )
        return super()._instruction(instruction)

    def _masked_clip(self):
        # The caller's soft mask belongs to the final colorized pattern paint,
        # not to the intermediate shape surface. Applying it here and again at
        # final composition would square the mask alpha. Cell-local non-None
        # soft masks are rejected above, so ignoring soft_mask during shape
        # generation is unambiguous.
        if getattr(self, "_uncolored_pattern_depth", 0):
            return None
        return super()._masked_clip()
