"""PDF text-state interpreter and TrueType glyph rasterization."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable

from .pdf_font import PDFTextFont, PDFFontError
from .raster import Color, Matrix, Path, Surface, rasterize_fill


class TextRenderError(ValueError):
    pass


@dataclass(slots=True)
class TextState:
    text_matrix: Matrix = Matrix()
    line_matrix: Matrix = Matrix()
    font: PDFTextFont | None = None
    font_size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    horizontal_scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0
    render_mode: int = 0
    clip_path: Path = field(default_factory=Path)

    def reset_matrices(self) -> None:
        self.text_matrix = Matrix()
        self.line_matrix = Matrix()
        self.clip_path.clear()

    def set_matrix(self, matrix: Matrix) -> None:
        self.text_matrix = matrix
        self.line_matrix = matrix

    def move_line(self, tx: float, ty: float) -> None:
        # Translation is applied in the text-line coordinate system.
        translation = Matrix(1, 0, 0, 1, tx, ty)
        self.line_matrix = self.line_matrix.concat(translation)
        self.text_matrix = self.line_matrix

    def next_line(self) -> None:
        self.move_line(0.0, -self.leading)

    def translate_text(self, tx: float, ty: float = 0.0) -> None:
        self.text_matrix = self.text_matrix.concat(Matrix(1, 0, 0, 1, tx, ty))


@dataclass(frozen=True, slots=True)
class TextPaintStyle:
    fill: Color
    stroke: Color
    line_width: float
    blend_mode: str = "Normal"


class TrueTypeTextRenderer:
    """Apply PDF text-show semantics to an owned raster surface."""

    def __init__(self, surface: Surface, *, ctm: Matrix) -> None:
        self.surface = surface
        self.ctm = ctm
        self.state = TextState()
        self.in_text_object = False

    def begin_text(self) -> None:
        self.state.reset_matrices()
        self.in_text_object = True

    def end_text(self) -> None:
        self._apply_text_clip()
        self.in_text_object = False

    def set_font(self, font: PDFTextFont, size: float) -> None:
        if size == 0:
            raise TextRenderError("text font size cannot be zero")
        self.state.font = font
        self.state.font_size = float(size)

    def set_text_matrix(self, matrix: Matrix) -> None:
        self._require_text()
        self.state.set_matrix(matrix)

    def move_text(self, tx: float, ty: float) -> None:
        self._require_text()
        self.state.move_line(tx, ty)

    def move_text_set_leading(self, tx: float, ty: float) -> None:
        self.state.leading = -float(ty)
        self.move_text(tx, ty)

    def next_line(self) -> None:
        self._require_text()
        self.state.next_line()

    def set_char_spacing(self, value: float) -> None:
        self.state.char_spacing = float(value)

    def set_word_spacing(self, value: float) -> None:
        self.state.word_spacing = float(value)

    def set_horizontal_scale_percent(self, value: float) -> None:
        self.state.horizontal_scale = float(value) / 100.0

    def set_leading(self, value: float) -> None:
        self.state.leading = float(value)

    def set_rise(self, value: float) -> None:
        self.state.rise = float(value)

    def set_render_mode(self, mode: int) -> None:
        if not 0 <= mode <= 7:
            raise TextRenderError(f"invalid PDF text rendering mode {mode}")
        self.state.render_mode = int(mode)

    def show(self, data: bytes, style: TextPaintStyle) -> None:
        self._require_text()
        font = self._require_font()
        for item in font.decode(data):
            self._paint_glyph(item.glyph_id, style)
            advance = (
                item.width_1000 / 1000.0 * self.state.font_size
                + self.state.char_spacing
                + (self.state.word_spacing if item.word_space else 0.0)
            ) * self.state.horizontal_scale
            self.state.translate_text(advance)

    def show_array(self, values: list[object], style: TextPaintStyle) -> None:
        self._require_text()
        for value in values:
            if isinstance(value, bytes):
                self.show(value, style)
            elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                adjustment = (
                    -float(value)
                    / 1000.0
                    * self.state.font_size
                    * self.state.horizontal_scale
                )
                self.state.translate_text(adjustment)
            else:
                raise TextRenderError(f"TJ array contains unsupported value {value!r}")

    def quote(self, data: bytes, style: TextPaintStyle) -> None:
        self.next_line()
        self.show(data, style)

    def double_quote(
        self,
        word_spacing: float,
        char_spacing: float,
        data: bytes,
        style: TextPaintStyle,
    ) -> None:
        self.set_word_spacing(word_spacing)
        self.set_char_spacing(char_spacing)
        self.next_line()
        self.show(data, style)

    def _glyph_transform(self) -> Matrix:
        font = self._require_font()
        units = font.sfnt.units_per_em
        text_scale = Matrix(
            self.state.font_size * self.state.horizontal_scale / units,
            0,
            0,
            self.state.font_size / units,
            0,
            self.state.rise,
        )
        return self.ctm.concat(self.state.text_matrix).concat(text_scale)

    def _paint_glyph(self, glyph_id: int, style: TextPaintStyle) -> None:
        font = self._require_font()
        transform = self._glyph_transform()
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
                stroke_width=max(0.01, style.line_width * self.ctm.expansion),
                blend_mode=style.blend_mode,
            )
        if clip:
            self._append_clip_path(glyph_path)

    def _append_clip_path(self, glyph: Path) -> None:
        for subpath in glyph.subpaths:
            if not subpath.points:
                continue
            self.state.clip_path.move_to(*subpath.points[0])
            for point in subpath.points[1:]:
                self.state.clip_path.line_to(*point)
            if subpath.closed:
                self.state.clip_path.close()

    def _apply_text_clip(self) -> None:
        if not self.state.clip_path.subpaths:
            return
        mask = rasterize_fill(
            self.state.clip_path,
            self.surface.width,
            self.surface.height,
            even_odd=False,
        )
        self.surface.apply_clip_mask(mask)
        self.state.clip_path.clear()

    def _require_text(self) -> None:
        if not self.in_text_object:
            raise TextRenderError("text operator used outside BT/ET")

    def _require_font(self) -> PDFTextFont:
        if self.state.font is None:
            raise TextRenderError("text-show operator used before Tf selected a font")
        return self.state.font
