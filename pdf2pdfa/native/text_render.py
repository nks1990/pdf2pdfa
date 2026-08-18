"""PDF text-state interpreter with owned TrueType and Type 3 painting."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Protocol

from .pdf_font import PDFTextFont, PDFFontError
from .raster import Color, Matrix, Path, Surface, rasterize_fill
from .type3_font import Type3FontError, Type3GlyphItem, Type3TextFont


class TextRenderError(ValueError):
    pass


class _TextFont(Protocol):
    is_type3: bool

    def decode(self, data: bytes) -> list[object]: ...


Type3Painter = Callable[
    [Type3TextFont, Type3GlyphItem, Matrix, "TextPaintStyle", int],
    None,
]


@dataclass(slots=True)
class TextState:
    text_matrix: Matrix = Matrix()
    line_matrix: Matrix = Matrix()
    font: PDFTextFont | Type3TextFont | None = None
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
    """Apply PDF text-show semantics to owned TrueType and Type 3 resources.

    The historical class name is retained to keep internal imports stable. Type
    3 glyph painting is delegated back to the page interpreter because a Type 3
    glyph is itself a PDF content stream and therefore needs the page graphics
    stack rather than an outline-only rasterizer.
    """

    def __init__(
        self,
        surface: Surface,
        *,
        ctm: Matrix,
        type3_painter: Type3Painter | None = None,
    ) -> None:
        self.surface = surface
        self.ctm = ctm
        self.type3_painter = type3_painter
        self.state = TextState()
        self.in_text_object = False

    def begin_text(self) -> None:
        self.state.reset_matrices()
        self.in_text_object = True

    def end_text(self) -> None:
        self._apply_text_clip()
        self.in_text_object = False

    def set_font(self, font: PDFTextFont | Type3TextFont, size: float) -> None:
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

    def _advance_item(self, item: object) -> None:
        spacing = self.state.char_spacing + (
            self.state.word_spacing if bool(getattr(item, "word_space", False)) else 0.0
        )
        if isinstance(item, Type3GlyphItem):
            # FontMatrix maps the Type 3 horizontal character-space width vector
            # into text space. Horizontal scaling affects the text-space X
            # component and spacing, while font size scales both dimensions.
            tx = (
                item.advance_x * self.state.font_size + spacing
            ) * self.state.horizontal_scale
            ty = item.advance_y * self.state.font_size
            self.state.translate_text(tx, ty)
            return

        width_1000 = getattr(item, "width_1000", None)
        if width_1000 is None:
            raise TextRenderError("font decoder returned glyph without width_1000")
        tx = (
            float(width_1000) / 1000.0 * self.state.font_size + spacing
        ) * self.state.horizontal_scale
        self.state.translate_text(tx)

    def show(self, data: bytes, style: TextPaintStyle) -> None:
        self._require_text()
        font = self._require_font()
        try:
            items = font.decode(data)
        except (PDFFontError, Type3FontError) as exc:
            raise TextRenderError(str(exc)) from exc
        for item in items:
            self._paint_item(item, style)
            self._advance_item(item)

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

    def _true_type_transform(self) -> Matrix:
        font = self._require_font()
        if not isinstance(font, PDFTextFont):
            raise TextRenderError("TrueType transform requested for non-TrueType font")
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

    def _type3_transform(self, font: Type3TextFont) -> Matrix:
        text_scale = Matrix(
            self.state.font_size * self.state.horizontal_scale,
            0,
            0,
            self.state.font_size,
            0,
            self.state.rise,
        )
        return (
            self.ctm
            .concat(self.state.text_matrix)
            .concat(text_scale)
            .concat(font.font_matrix)
        )

    def _paint_item(self, item: object, style: TextPaintStyle) -> None:
        font = self._require_font()
        if isinstance(font, Type3TextFont):
            mode = self.state.render_mode
            if mode == 3:
                return
            if mode != 0:
                raise TextRenderError(
                    "Type3 text rendering modes other than fill(0) and invisible(3) "
                    "require dedicated owned stroke/clip semantics"
                )
            if not isinstance(item, Type3GlyphItem):
                raise TextRenderError("Type3 decoder returned an invalid glyph item")
            if self.type3_painter is None:
                raise TextRenderError("Type3 glyph requires page-interpreter callback")
            self.type3_painter(
                font,
                item,
                self._type3_transform(font),
                style,
                mode,
            )
            return

        glyph_id = getattr(item, "glyph_id", None)
        if not isinstance(glyph_id, int):
            raise TextRenderError("TrueType decoder returned glyph without glyph_id")
        self._paint_true_type_glyph(glyph_id, style)

    def _paint_true_type_glyph(self, glyph_id: int, style: TextPaintStyle) -> None:
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

    def _require_font(self) -> PDFTextFont | Type3TextFont:
        if self.state.font is None:
            raise TextRenderError("text-show operator used before Tf selected a font")
        return self.state.font
