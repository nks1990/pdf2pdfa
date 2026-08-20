"""Canonical owned page renderer with affine strokes and Type 3 charprocs."""

from __future__ import annotations

import copy
from pathlib import Path as FSPath

from .affine_stroke import StrokeError, stroke_affine
from .content import ContentInstruction, InlineImage, parse_content_stream
from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject
from .page_render import (
    PageRenderer,
    RenderedPage,
    RenderingError,
    UnsupportedRenderingError,
    _name,
    _number,
    _resolve_resource,
)
from .pdf_font import PDFTextFont, PDFFontError
from .raster import Color, Matrix
from .structure import decoded_stream_bytes, resolve, walk_pages
from .type3_font import Type3FontError, Type3GlyphItem, Type3TextFont


_TEXT_OPERATORS = {
    "BT", "ET", "Tf", "Tm", "Td", "TD", "T*", "Tc", "Tw", "Tz",
    "TL", "Ts", "Tr", "Tj", "TJ", "'", '"',
}


class OwnedPageRenderer(PageRenderer):
    def __init__(self, doc: PDFDocument, *, dpi: int = 144) -> None:
        super().__init__(doc, dpi=dpi)
        self._path_ctm: Matrix | None = None
        self._mixed_path_ctm = False
        self._type3_depth = 0

    def render_page(self, page):
        self._path_ctm = None
        self._mixed_path_ctm = False
        self._type3_depth = 0
        return super().render_page(page)

    def _instruction(self, instruction: ContentInstruction) -> None:
        if self.text is not None and self.text.type3_painter is None:
            self.text.type3_painter = self._paint_type3_glyph

        op = instruction.operator
        args = list(instruction.operands)

        if self._type3_depth:
            if op in {"d0", "d1"}:
                expected = 2 if op == "d0" else 6
                if len(args) != expected:
                    raise RenderingError(f"Type3 {op} expects {expected} numbers")
                numbers = [_number(value, f"Type3 {op}") for value in args]
                if op == "d1" and (numbers[4] < numbers[2] or numbers[5] < numbers[3]):
                    raise RenderingError("Type3 d1 glyph bounding box is invalid")
                return
            if op in _TEXT_OPERATORS:
                raise UnsupportedRenderingError(
                    "nested text inside a Type3 CharProc requires an owned nested text-state renderer"
                )
        elif op in {"d0", "d1"}:
            raise UnsupportedRenderingError(
                f"{op} is valid only while interpreting an owned Type3 CharProc"
            )

        if op == "Tf" and not self._type3_depth:
            if len(args) != 2:
                raise RenderingError("Tf expects font name and size")
            font_value = _resolve_resource(
                self.doc,
                self.resources,
                "Font",
                _name(args[0], "Tf"),
            )
            subtype = None
            if isinstance(font_value, PDFDict):
                raw_subtype = resolve(self.doc, font_value.get("Subtype"))
                if isinstance(raw_subtype, PDFName):
                    subtype = raw_subtype.value
            if subtype == "Type3":
                try:
                    font = Type3TextFont(self.doc, font_value)
                except Type3FontError as exc:
                    raise UnsupportedRenderingError(str(exc)) from exc
                _, _, text = self._require()
                text.set_font(font, _number(args[1], "Tf"))
                return

        return super()._instruction(instruction)

    @staticmethod
    def _validate_type3_charproc(content: bytes, glyph_name: str) -> None:
        try:
            items = list(parse_content_stream(content))
        except Exception as exc:
            raise RenderingError(f"Type3 /{glyph_name} CharProc is invalid: {exc}") from exc
        if not items:
            raise RenderingError(f"Type3 /{glyph_name} CharProc is empty")
        first = items[0]
        if not isinstance(first, ContentInstruction) or first.operator not in {"d0", "d1"}:
            raise RenderingError(
                f"Type3 /{glyph_name} CharProc shall begin with d0 or d1"
            )
        balance = 0
        for item in items:
            if isinstance(item, InlineImage):
                continue
            if item.operator == "q":
                balance += 1
            elif item.operator == "Q":
                balance -= 1
                if balance < 0:
                    raise RenderingError(
                        f"Type3 /{glyph_name} CharProc has Q without matching q"
                    )
        if balance:
            raise RenderingError(
                f"Type3 /{glyph_name} CharProc has unbalanced q/Q graphics state"
            )

    def _paint_type3_glyph(
        self,
        font: Type3TextFont,
        item: Type3GlyphItem,
        glyph_ctm: Matrix,
        style,
        render_mode: int,
    ) -> None:
        # PDF text rendering mode is intentionally ignored for Type3. The
        # CharProc itself controls painting, and Tr 4..7 do not contribute a
        # Type3 glyph outline to the text clipping path.
        del style, render_mode
        if self._type3_depth >= 16:
            raise RenderingError("Type3 CharProc recursion exceeds 16")

        try:
            charproc = font.charproc(item.char_name)
            content = decoded_stream_bytes(
                self.doc,
                charproc,
                label=f"Type3 CharProc /{item.char_name}",
            )
        except Type3FontError as exc:
            raise RenderingError(str(exc)) from exc
        self._validate_type3_charproc(content, item.char_name)

        surface, state, text = self._require()
        saved_path = copy.deepcopy(self.path)
        saved_pending_clip = self.pending_clip
        saved_resources = self.resources
        saved_path_ctm = self._path_ctm
        saved_mixed_path_ctm = self._mixed_path_ctm

        super()._instruction(ContentInstruction((), "q", 0, 0))
        synthetic_depth = len(self.stack)
        try:
            self._type3_depth += 1
            self.path.clear()
            self.pending_clip = None
            self._path_ctm = None
            self._mixed_path_ctm = False
            state = self.state
            assert state is not None
            state.ctm = glyph_ctm
            text.ctm = glyph_ctm
            self.resources = font.resources
            self._execute(content, font.resources)
            if len(self.stack) != synthetic_depth:
                raise RenderingError(
                    f"Type3 /{item.char_name} CharProc changed graphics-stack depth"
                )
        finally:
            self._type3_depth -= 1
            while len(self.stack) > synthetic_depth:
                super()._instruction(ContentInstruction((), "Q", 0, 0))
            if len(self.stack) == synthetic_depth:
                super()._instruction(ContentInstruction((), "Q", 0, 0))
            else:
                raise RenderingError(
                    f"Type3 /{item.char_name} CharProc escaped its graphics-state frame"
                )
            self.resources = saved_resources
            self.path = saved_path
            self.pending_clip = saved_pending_clip
            self._path_ctm = saved_path_ctm
            self._mixed_path_ctm = saved_mixed_path_ctm

    def _path_operator(self, op: str, args: list[PDFObject]) -> None:
        _, state, _ = self._require()
        if op != "h":
            if self._path_ctm is None:
                self._path_ctm = state.ctm
            elif self._path_ctm != state.ctm:
                self._mixed_path_ctm = True
        super()._path_operator(op, args)

    def _paint_operator(self, op: str, args: list[PDFObject]) -> None:
        if args:
            raise RenderingError(f"{op} takes no operands")
        surface, state, _ = self._require()
        close = op in {"s", "b", "b*"}
        if close and self.path.subpaths:
            self.path.close()
        fill = op in {"f", "F", "f*", "B", "B*", "b", "b*"}
        stroke = op in {"S", "s", "B", "B*", "b", "b*"}
        even_odd = op in {"f*", "B*", "b*"}

        # W/W* modifies the clipping path for the same path object when that
        # path is terminated by a painting operator (or n). Applying it after
        # paint would incorrectly let B/S/f escape the newly selected clip.
        self._apply_pending_clip()

        if fill:
            surface.fill_path(
                self.path,
                Color(
                    state.fill_color.r,
                    state.fill_color.g,
                    state.fill_color.b,
                    state.fill_alpha,
                ),
                even_odd=even_odd,
                blend_mode=state.blend_mode,
            )
        if stroke:
            path_ctm = self._path_ctm or state.ctm
            if self._mixed_path_ctm or path_ctm != state.ctm:
                raise UnsupportedRenderingError(
                    "stroke path constructed under multiple/different CTMs requires segmented affine stroke state"
                )
            try:
                stroke_affine(
                    surface,
                    self.path,
                    path_ctm=path_ctm,
                    line_width=state.line_width,
                    line_cap=state.line_cap,
                    line_join=state.line_join,
                    miter_limit=state.miter_limit,
                    dash_array=state.dash_array,
                    dash_phase=state.dash_phase,
                    color=Color(
                        state.stroke_color.r,
                        state.stroke_color.g,
                        state.stroke_color.b,
                        state.stroke_alpha,
                    ),
                    blend_mode=state.blend_mode,
                )
            except StrokeError as exc:
                raise RenderingError(str(exc)) from exc

        self.path.clear()
        self.pending_clip = None
        self._path_ctm = None
        self._mixed_path_ctm = False

    def _show_text(self, op: str, args: list[PDFObject]) -> None:
        _, state, text = self._require()
        # Tr stroke modes do not turn a Type3 CharProc into outline-stroked
        # text; therefore the affine-stroke guard is irrelevant to Type3.
        if (
            not isinstance(text.state.font, Type3TextFont)
            and text.state.render_mode in (1, 2, 5, 6)
        ):
            a, b, c, d = state.ctm.a, state.ctm.b, state.ctm.c, state.ctm.d
            l1 = (a * a + b * b) ** 0.5
            l2 = (c * c + d * d) ** 0.5
            dot = a * c + b * d
            tolerance = 1e-7 * max(1.0, l1, l2)
            if abs(l1 - l2) > tolerance or abs(dot) > tolerance:
                raise UnsupportedRenderingError(
                    "stroked text under non-uniform/skew CTM requires affine glyph stroke outlines"
                )
        super()._show_text(op, args)


def render_page(
    source: str | FSPath | bytes | PDFDocument,
    page_number: int = 1,
    *,
    dpi: int = 144,
) -> RenderedPage:
    doc = source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=True)
    if page_number <= 0:
        raise ValueError("page_number is 1-based")
    for index, page in enumerate(walk_pages(doc), start=1):
        if index == page_number:
            return OwnedPageRenderer(doc, dpi=dpi).render_page(page)
    raise IndexError(f"PDF has fewer than {page_number} pages")
