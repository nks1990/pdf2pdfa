"""Page-renderer mixin adding owned CCITT image decoding."""

from __future__ import annotations

from .content import InlineImage
from .image import ImageError
from .image_ccitt import decode_image_owned
from .objects import PDFStream
from .page_render import (
    RenderingError,
    UnsupportedRenderingError,
    _inline_dictionary,
    _name,
    _resolve_resource,
)
from .structure import resolve


class CCITTImageRendererMixin:
    def _xobject(self, name: str) -> None:
        value = _resolve_resource(self.doc, self.resources, "XObject", name)  # type: ignore[attr-defined]
        if not isinstance(value, PDFStream):
            raise RenderingError("XObject is not a stream")
        subtype = _name(resolve(self.doc, value.get("Subtype")), "XObject/Subtype")  # type: ignore[attr-defined]
        if subtype != "Image":
            return super()._xobject(name)  # type: ignore[misc]
        _, state, _ = self._require()  # type: ignore[attr-defined]
        try:
            image = decode_image_owned(
                self.doc,  # type: ignore[attr-defined]
                value,
                resources=self.resources,  # type: ignore[attr-defined]
            )
        except ImageError as exc:
            raise UnsupportedRenderingError(str(exc)) from exc
        self._draw_image(image, state.ctm)  # type: ignore[attr-defined]

    def _inline_image(self, inline: InlineImage) -> None:
        _, state, _ = self._require()  # type: ignore[attr-defined]
        stream = PDFStream(_inline_dictionary(inline.dictionary), inline.data)
        try:
            image = decode_image_owned(
                self.doc,  # type: ignore[attr-defined]
                stream,
                resources=self.resources,  # type: ignore[attr-defined]
            )
        except ImageError as exc:
            raise UnsupportedRenderingError(str(exc)) from exc
        self._draw_image(image, state.ctm)  # type: ignore[attr-defined]
