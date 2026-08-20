"""Page-renderer bridge for embedded PDF Type1C/CIDFontType0C fonts."""

from __future__ import annotations

from .cff_pdf_font import CFFPDFFontError, CFFPDFTextFont
from .cff_text_render import OwnedOutlineTextRenderer
from .content import ContentInstruction
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError, _name, _number, _resolve_resource
from .structure import resolve


def _dict(value: PDFObject) -> PDFDict | None:
    return value if isinstance(value, PDFDict) else None


def _fontfile3_subtype(doc, descriptor: PDFDict | None) -> str:
    if descriptor is None or descriptor.get("FontFile3") is None:
        return ""
    value = resolve(doc, descriptor.get("FontFile3"))
    if not isinstance(value, PDFStream):
        return ""
    subtype = resolve(doc, value.get("Subtype")) if value.get("Subtype") is not None else None
    return subtype.value if isinstance(subtype, PDFName) else ""


def _is_owned_cff_font(doc, font_value: PDFObject) -> bool:
    font = resolve(doc, font_value)
    if not isinstance(font, PDFDict):
        return False
    subtype = resolve(doc, font.get("Subtype"))
    subtype_name = subtype.value if isinstance(subtype, PDFName) else ""
    if subtype_name == "Type1":
        descriptor = resolve(doc, font.get("FontDescriptor")) if font.get("FontDescriptor") is not None else None
        return _fontfile3_subtype(doc, descriptor if isinstance(descriptor, PDFDict) else None) == "Type1C"
    if subtype_name != "Type0":
        return False
    descendants = resolve(doc, font.get("DescendantFonts")) if font.get("DescendantFonts") is not None else None
    if not isinstance(descendants, list) or len(descendants) != 1:
        return False
    descendant = resolve(doc, descendants[0])
    if not isinstance(descendant, PDFDict):
        return False
    cid_subtype = resolve(doc, descendant.get("Subtype"))
    if not isinstance(cid_subtype, PDFName) or cid_subtype.value != "CIDFontType0":
        return False
    descriptor = resolve(doc, descendant.get("FontDescriptor")) if descendant.get("FontDescriptor") is not None else None
    return _fontfile3_subtype(doc, descriptor if isinstance(descriptor, PDFDict) else None) == "CIDFontType0C"


class CFFTextPageRendererMixin:
    """Install the full outline text renderer and intercept CFF ``Tf`` only."""

    def _ensure_outline_text_renderer(self) -> OwnedOutlineTextRenderer:
        if self.text is None:  # type: ignore[attr-defined]
            raise RenderingError("renderer has no active text state")
        if isinstance(self.text, OwnedOutlineTextRenderer):  # type: ignore[attr-defined]
            return self.text  # type: ignore[return-value]
        surface, _, old = self._require()  # type: ignore[attr-defined]
        replacement = OwnedOutlineTextRenderer(
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
        if not _is_owned_cff_font(self.doc, font_value):  # type: ignore[attr-defined]
            return super()._instruction(instruction)  # type: ignore[misc]
        try:
            font = CFFPDFTextFont(self.doc, font_value)  # type: ignore[attr-defined]
        except CFFPDFFontError as exc:
            raise UnsupportedRenderingError(str(exc)) from exc
        text.set_font(font, _number(args[1], "Tf"))
