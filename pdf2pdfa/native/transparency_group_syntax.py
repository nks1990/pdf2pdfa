"""Strict syntax guard shared by owned transparency-group compositors."""

from __future__ import annotations

from .objects import PDFDict
from .page_render import RenderingError
from .structure import resolve


class TransparencyGroupSyntaxMixin:
    def _group_flags(self, group: PDFDict) -> tuple[bool, bool]:
        values: list[bool] = []
        for key in ("I", "K"):
            if group.get(key) is None:
                values.append(False)
                continue
            raw = resolve(self.doc, group.get(key))
            if not isinstance(raw, bool):
                raise RenderingError(f"transparency Group /{key} shall be boolean")
            values.append(raw)
        return values[0], values[1]
