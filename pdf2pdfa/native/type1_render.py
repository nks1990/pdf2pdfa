"""Page-renderer bridge for embedded PDF `/Type1 + FontFile` fonts.

This mixin makes the owned Type 1 core production-reachable without creating a
second PDF text-state machine.  It upgrades the page renderer to
``FullOutlineTextRenderer`` and intercepts only ``Tf`` selections that actually
point at an embedded `/FontFile`.  Type1C, TrueType and Type3 continue through
the existing owned paths.
"""

from __future__ import annotations

from .content import ContentInstruction
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError, _name, _number, _resolve_resource
from .structure import resolve
from .type1_pdf_font import Type1PDFFontError, Type1PDFTextFont
from .type1_text_render import FullOutlineTextRenderer


def _is_owned_type1_font(doc, font_value: PDFObject) -> bool:
    font = resolve(doc, font_value)
    if not isinstance(font, PDFDict):
        return False
    subtype = resolve(doc, font.get("Subtype")) if font.get("Subtype") is not None else None
    if not isinstance(subtype, PDFName) or subtype.value != "Type1":
        return False
    descriptor = resolve(doc, font.get("FontDescriptor")) if font.get("FontDescriptor") is not None else None
    if not isinstance(descriptor, PDFDict):
        return False
    fontfile = resolve(doc, descriptor.get("FontFile")) if descriptor.get("FontFile") is not None else None
    return isinstance(fontfile, PDFStream)


class Type1TextPageRendererMixin:
    """Install the full outline renderer and intercept embedded Type1 ``Tf``."""

    def _ensure_outline_text_renderer(self) -> FullOutlineTextRenderer:
        if self.text is None:  # type: ignore[attr-defined]
            raise RenderingError("renderer has no active text state")
        if isinstance(self.text, FullOutlineTextRenderer):  # type: ignore[attr-defined]
            return self.text  # type: ignore[return-value]
        surface, _, old = self._require()  # type: ignore[attr-defined]
        replacement = FullOutlineTextRenderer(
            surface,
            ctm=old.ctm,
            type3_painter=old.type3_painter,
        )
        replacement.state = old.state
        replacement.in_text_object = old.in_text_object
        self.text = replacement  # type: ignore[attr-defined]
        return replacement

    def _instruction(self, instruction: ContentInstruction) -> None:
        text = self._ensure_outline_text_renderer()
        if instruction.operator != "Tf" or getattr(self, "_type3_depth", 0):
            return super()._instruction(instruction)  # type: ignore[misc]

        args = list(instruction.operands)
        if len(args) != 2:
            raise RenderingError("Tf expects font name and size")
        font_value = _resolve_resource(
            self.doc,  # type: ignore[attr-defined]
            self.resources,  # type: ignore[attr-defined]
            "Font",
            _name(args[0], "Tf"),
        )
        if not _is_owned_type1_font(self.doc, font_value):  # type: ignore[attr-defined]
            return super()._instruction(instruction)  # type: ignore[misc]
        try:
            font = Type1PDFTextFont(self.doc, font_value)  # type: ignore[attr-defined]
        except Type1PDFFontError as exc:
            raise UnsupportedRenderingError(str(exc)) from exc
        text.set_font(font, _number(args[1], "Tf"))
