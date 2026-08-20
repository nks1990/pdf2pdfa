"""Outline-aware extension of the owned PDF text-state renderer.

The canonical renderer uses this class for TrueType, CFF1 and Type3.  It also
owns Type0 writing-mode-1 placement: vertical-origin offsets, Y displacement
and vertical TJ adjustments are applied in one text-state machine shared by
CIDFontType2 and CIDFontType0C.
"""

from __future__ import annotations

from decimal import Decimal

from .cff_pdf_font import CFFGlyphItem, CFFPDFFontError, CFFPDFTextFont
from .pdf_font import GlyphItem, PDFTextFont
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

    def _advance_item(self, item: object) -> None:
        metric = getattr(item, "vertical_metric", None)
        if metric is None:
            return super()._advance_item(item)
        spacing = self.state.char_spacing + (
            self.state.word_spacing if bool(getattr(item, "word_space", False)) else 0.0
        )
        ty = float(metric.displacement_y) / 1000.0 * self.state.font_size + spacing
        self.state.translate_text(0.0, ty)

    def show_array(self, values: list[object], style: TextPaintStyle) -> None:
        self._require_text()
        font = self._require_font()
        if not bool(getattr(font, "vertical", False)):
            return super().show_array(values, style)
        for value in values:
            if isinstance(value, bytes):
                self.show(value, style)
            elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                # In WMode 1 the text-space displacement vector is vertical;
                # horizontal scaling therefore does not scale this TJ correction.
                adjustment = -float(value) / 1000.0 * self.state.font_size
                self.state.translate_text(0.0, adjustment)
            else:
                raise TextRenderError(f"TJ array contains unsupported value {value!r}")

    def _vertical_offset(self, item: object) -> tuple[float, float]:
        metric = getattr(item, "vertical_metric", None)
        if metric is None:
            return 0.0, self.state.rise
        return (
            -float(metric.position_x) / 1000.0
            * self.state.font_size * self.state.horizontal_scale,
            self.state.rise
            - float(metric.position_y) / 1000.0 * self.state.font_size,
        )

    def _cff_transform(self, item: CFFGlyphItem) -> Matrix:
        offset_x, offset_y = self._vertical_offset(item)
        text_scale = Matrix(
            self.state.font_size * self.state.horizontal_scale,
            0,
            0,
            self.state.font_size,
            offset_x,
            offset_y,
        )
        return self.ctm.concat(self.state.text_matrix).concat(text_scale)

    def _true_type_item_transform(self, font: PDFTextFont, item: GlyphItem) -> Matrix:
        units = font.sfnt.units_per_em
        offset_x, offset_y = self._vertical_offset(item)
        text_scale = Matrix(
            self.state.font_size * self.state.horizontal_scale / units,
            0,
            0,
            self.state.font_size / units,
            offset_x,
            offset_y,
        )
        return self.ctm.concat(self.state.text_matrix).concat(text_scale)

    def _paint_item(self, item: object, style: TextPaintStyle) -> None:
        font = self._require_font()
        if isinstance(font, CFFPDFTextFont):
            if not isinstance(item, CFFGlyphItem):
                raise TextRenderError("CFF decoder returned an invalid glyph item")
            try:
                glyph_path = font.glyph_path(item.glyph_id, self._cff_transform(item))
            except CFFPDFFontError as exc:
                raise TextRenderError(str(exc)) from exc
            return self._paint_outline_path(glyph_path, style)

        if isinstance(font, PDFTextFont) and isinstance(item, GlyphItem) and item.vertical_metric is not None:
            glyph_path = font.outlines.path(
                item.glyph_id,
                self._true_type_item_transform(font, item),
            )
            return self._paint_outline_path(glyph_path, style)

        return super()._paint_item(item, style)

    def _paint_outline_path(self, glyph_path, style: TextPaintStyle) -> None:
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
        """Canonical horizontal TrueType text paint with correct stroke API."""
        font = self._require_font()
        if not isinstance(font, PDFTextFont):
            raise TextRenderError("TrueType glyph requested for non-TrueType font")
        transform = self._true_type_transform()
        glyph_path = font.outlines.path(glyph_id, transform)
        self._paint_outline_path(glyph_path, style)
