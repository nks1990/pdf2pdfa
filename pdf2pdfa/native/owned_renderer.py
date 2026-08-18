"""Highest-capability owned page renderer composition.

This is the renderer used by fidelity/flattening. It composes owned CCITT image
decoding, Type 3 CharProc execution, transparency/soft-mask state and
axial/radial shading painting in a single fail-closed interpreter.
"""

from __future__ import annotations

from pathlib import Path as FSPath

from .ccitt_render import CCITTImageRendererMixin
from .document import PDFDocument
from .page_render import RenderedPage
from .shading_render import ShadingRendererMixin
from .transparency_render import TransparencyRenderer
from .structure import walk_pages


class FullOwnedPageRenderer(
    CCITTImageRendererMixin,
    ShadingRendererMixin,
    TransparencyRenderer,
):
    def _paint_type3_glyph(self, *args, **kwargs) -> None:
        saved_soft = bytearray(self.soft_mask) if self.soft_mask is not None else None
        saved_soft_stack = [
            bytearray(item) if item is not None else None
            for item in self._soft_stack
        ]
        try:
            super()._paint_type3_glyph(*args, **kwargs)
        finally:
            self.soft_mask = saved_soft
            self._soft_stack[:] = saved_soft_stack


def render_page_full(
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
            return FullOwnedPageRenderer(doc, dpi=dpi).render_page(page)
    raise IndexError(f"PDF has fewer than {page_number} pages")
