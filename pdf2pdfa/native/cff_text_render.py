"""CFF-aware extension of the owned PDF text-state renderer.

The canonical renderer installs this class for all text, not just CFF. Besides
adding original-program CFF painting, it fixes the historical TrueType text
stroke call so render modes 1/2/5/6 use ``Surface.stroke_path(width=...)``.
"""

from __future__ import annotations

from .cff_pdf_font import CFFGlyphItem, CFFPDFFontError, CFFPDFTextFont
from .pdf_font import PDFTextFont
from .raster import Matrix
from .text_render import TextPaintStyle, TextRenderError, TrueTypeTextRenderer


class OwnedOutlineTextRenderer(TrueTypeTextRenderer):
    """Render TrueType, CFF1 and Type3 through one PDF text-state machine."""

    def show(self, data: bytes, style: TextPaintStyle) -> None:
        self._require_text()
        font = self._require_font()
        if not isinstance(font, CFFPDFTextFont):
            return super().show(data, style)
        try:
            items = font.decode(data)
        except CFFPDFFontError as exc:
            raise TextRenderError(str(exc)) from exc
        for item in items:
            self._paint_item(item, style)
            self._advance_item(item)

    def _cff_transform(self) -> Matrix:
        text_scale = Matrix(
            self.state.font_size * self.state.horizontal_scale,
            0,
            0,
            self.state.font_size,
            0,
            self.state.rise,
        )
        return self.ctm.concat(self.state.text_matrix).concat(text_scale)

    def _paint_item(self, item: object, style: TextPaintStyle) -> None:
        font = self._require_font()
        if not isinstance(font, CFFPDFTextFont):
            return super()._paint_item(item, style)
        if not isinstance(item, CFFGlyphItem):
            raise TextRenderError("CFF decoder returned an invalid glyph item")
        try:
            glyph_path = font.glyph_path(item.glyph_id, self._cff_transform())
        except CFFPDFFontError as exc:
            raise TextRenderError(str(exc)) from exc

        mode = self.state.render_mode
        fill = mode in (0, 2, 4, 6)
        stroke = mode in (1, 2, 5, 6)
        clip = mode in (4, 5, 6, 7)
        if fill:
            self.surface.fill_path(
                glyph_path,
                style.fill,
                even_odd=False,
                blend_mode=style.blend_mode,
            )
        if stroke:
            self.surface.stroke_path(
                glyph_path,
                style.stroke,
                width=max(0.01, style.line_width * self.ctm.expansion),
                blend_mode=style.blend_mode,
            )
        if clip:
            self._append_clip_path(glyph_path)

    def _paint_true_type_glyph(self, glyph_id: int, style: TextPaintStyle) -> None:
        """Canonical TrueType text paint with the correct raster stroke API."""
        font = self._require_font()
        if not isinstance(font, PDFTextFont):
            raise TextRenderError("TrueType glyph requested for non-TrueType font")
        transform = self._true_type_transform()
        glyph_path = font.outlines.path(glyph_id, transform)
        mode = self.state.render_mode
        fill = mode in (0, 2, 4, 6)
        stroke = mode in (1, 2, 5, 6)
        clip = mode in (4, 5, 6, 7)
        if fill:
            self.surface.fill_path(
                glyph_path,
                style.fill,
                even_odd=False,
                blend_mode=style.blend_mode,
            )
        if stroke:
            self.surface.stroke_path(
                glyph_path,
                style.stroke,
                width=max(0.01, style.line_width * self.ctm.expansion),
                blend_mode=style.blend_mode,
            )
        if clip:
            self._append_clip_path(glyph_path)
