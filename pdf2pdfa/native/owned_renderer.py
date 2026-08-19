"""Highest-capability owned page renderer composition.

This is the renderer used by visual fidelity and PDF/A-1 flattening. Every
renderer capability considered production-reachable must be composed here;
component modules that are not in this MRO are not treated as shipped support.
"""

from __future__ import annotations

from pathlib import Path as FSPath

from .annotation_render import AnnotationAppearanceRendererMixin
from .ccitt_render import CCITTImageRendererMixin
from .cff_render import CFFTextPageRendererMixin
from .document import PDFDocument
from .nonisolated_transparency import NonIsolatedTransparencyRendererMixin
from .page_render import RenderedPage
from .pattern_dispatch import CanonicalPatternShadingMixin
from .pattern_render import PatternShadingRendererMixin
from .shading_render import ShadingRendererMixin
from .structure import walk_pages
from .tiling_pattern import ColoredTilingPatternRendererMixin
from .transparency_group_syntax import TransparencyGroupSyntaxMixin
from .transparency_render import TransparencyRenderer
from .type1_render import Type1TextPageRendererMixin
from .uncolored_pattern import UncoloredTilingPatternRendererMixin
from .uncolored_pattern_safety import UncoloredPatternSafetyMixin


class FullOwnedPageRenderer(
    AnnotationAppearanceRendererMixin,
    CCITTImageRendererMixin,
    UncoloredPatternSafetyMixin,
    UncoloredTilingPatternRendererMixin,
    ColoredTilingPatternRendererMixin,
    CanonicalPatternShadingMixin,
    PatternShadingRendererMixin,
    ShadingRendererMixin,
    Type1TextPageRendererMixin,
    CFFTextPageRendererMixin,
    TransparencyGroupSyntaxMixin,
    NonIsolatedTransparencyRendererMixin,
    TransparencyRenderer,
):
    """Canonical renderer used by production conversion gates."""

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
    render_annotations: bool = True,
) -> RenderedPage:
    doc = source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=True)
    if page_number <= 0:
        raise ValueError("page_number is 1-based")
    for index, page in enumerate(walk_pages(doc), start=1):
        if index == page_number:
            return FullOwnedPageRenderer(
                doc,
                dpi=dpi,
                render_annotations=render_annotations,
            ).render_page(page)
    raise IndexError(f"PDF has fewer than {page_number} pages")
