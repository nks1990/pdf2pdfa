"""Canonical owned page renderer with affine-correct PDF strokes."""

from __future__ import annotations

from pathlib import Path as FSPath

from .affine_stroke import StrokeError, stroke_affine
from .document import PDFDocument
from .objects import PDFObject
from .page_render import (
    PageRenderer,
    RenderedPage,
    RenderingError,
    UnsupportedRenderingError,
)
from .raster import Color, Matrix
from .structure import walk_pages


class OwnedPageRenderer(PageRenderer):
    def __init__(self, doc: PDFDocument, *, dpi: int = 144) -> None:
        super().__init__(doc, dpi=dpi)
        self._path_ctm: Matrix | None = None
        self._mixed_path_ctm = False

    def render_page(self, page):
        self._path_ctm = None
        self._mixed_path_ctm = False
        return super().render_page(page)

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

        # PDF applies W/W* when the current path is ended. Painting uses the
        # clipping path that existed before this path; the new clip affects
        # subsequent graphics. Therefore paint first, then commit pending clip.
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

        self._apply_pending_clip()
        self.path.clear()
        self.pending_clip = None
        self._path_ctm = None
        self._mixed_path_ctm = False

    def _show_text(self, op: str, args: list[PDFObject]) -> None:
        _, state, text = self._require()
        if text.state.render_mode in (1, 2, 5, 6):
            # text_render currently converts line width to device scalar. Avoid
            # an incorrect result under an affine pen until glyph-stroke outline
            # is routed through stroke_affine as well.
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
