"""Type1-aware extension of the canonical owned outline text renderer."""

from __future__ import annotations

from .cff_text_render import OwnedOutlineTextRenderer
from .raster import Matrix
from .text_render import TextPaintStyle, TextRenderError
from .type1_pdf_font import Type1GlyphItem, Type1PDFFontError, Type1PDFTextFont


class FullOutlineTextRenderer(OwnedOutlineTextRenderer):
    """One text-state machine for TrueType, CFF, Type1 and Type3."""

    def show(self, data: bytes, style: TextPaintStyle) -> None:
        self._require_text()
        font = self._require_font()
        if not isinstance(font, Type1PDFTextFont):
            return super().show(data, style)
        try:
            items = font.decode(data)
        except Type1PDFFontError as exc:
            raise TextRenderError(str(exc)) from exc
        for item in items:
            self._paint_item(item, style)
            self._advance_item(item)

    def _type1_transform(self) -> Matrix:
        return self.ctm.concat(self.state.text_matrix).concat(
            Matrix(
                self.state.font_size * self.state.horizontal_scale,
                0,
                0,
                self.state.font_size,
                0,
                self.state.rise,
            )
        )

    def _paint_item(self, item: object, style: TextPaintStyle) -> None:
        font = self._require_font()
        if not isinstance(font, Type1PDFTextFont):
            return super()._paint_item(item, style)
        if not isinstance(item, Type1GlyphItem):
            raise TextRenderError("Type1 decoder returned an invalid glyph item")
        try:
            path = font.glyph_path(item.glyph_name, self._type1_transform())
        except Type1PDFFontError as exc:
            raise TextRenderError(str(exc)) from exc

        mode = self.state.render_mode
        fill = mode in (0, 2, 4, 6)
        stroke = mode in (1, 2, 5, 6)
        clip = mode in (4, 5, 6, 7)
        if fill:
            self.surface.fill_path(
                path,
                style.fill,
                even_odd=False,
                blend_mode=style.blend_mode,
            )
        if stroke:
            self.surface.stroke_path(
                path,
                style.stroke,
                width=max(0.01, style.line_width * self.ctm.expansion),
                blend_mode=style.blend_mode,
            )
        if clip:
            self._append_clip_path(path)
