"""Owned PDF/A-1 transparency flattening.

The flattener intentionally rasterizes only pages whose *used* painting
instructions require transparency. It renders through the owned transparency
renderer, embeds an opaque RGB image and replaces only that page's painting
content. Page boxes and /Rotate are preserved.

Annotation appearance streams are deliberately not flattened here. The repair
planner rejects those cases until annotation appearance composition is owned as
well; silently dropping or double-painting an annotation would be worse than a
hard failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .document import PDFDocument
from .filters import flate_encode
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError
from .structure import PageView, walk_pages
from .transparency_render import TransparencyRenderer


class TransparencyFlattenError(RuntimeError):
    """Raised when a page cannot be flattened without guessing."""


@dataclass(frozen=True, slots=True)
class FlattenedPage:
    page_number: int
    width: int
    height: int
    dpi: int


@dataclass(frozen=True, slots=True)
class FlattenReport:
    pages: tuple[FlattenedPage, ...]

    @property
    def count(self) -> int:
        return len(self.pages)


def _format_number(value: Decimal | int | float) -> str:
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        return str(value)
    else:
        number = Decimal(str(value))
    number = number.normalize()
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _unrotated(page: PageView) -> PageView:
    """Render in page user-space orientation and let /Rotate remain structural."""
    return PageView(
        ref=page.ref,
        dictionary=page.dictionary,
        resources=page.resources,
        media_box=page.media_box,
        crop_box=page.crop_box,
        rotate=0,
    )


def _image_stream(rgb: bytes, width: int, height: int) -> PDFStream:
    if len(rgb) != width * height * 3:
        raise TransparencyFlattenError("renderer returned an invalid RGB raster length")
    compressed = flate_encode(rgb)
    return PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": width,
                "Height": height,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerComponent": 8,
                "Filter": PDFName("FlateDecode"),
                "Interpolate": False,
            }
        ),
        compressed,
    )


def _replacement_content(page: PageView, resource_name: str) -> bytes:
    x0, y0, x1, y1 = page.crop_box
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        raise TransparencyFlattenError(f"page {page.ref} has an invalid CropBox")
    return (
        "q\n"
        f"{_format_number(width)} 0 0 {_format_number(height)} "
        f"{_format_number(x0)} {_format_number(y0)} cm\n"
        f"/{resource_name} Do\n"
        "Q\n"
    ).encode("ascii")


def flatten_pages(
    doc: PDFDocument,
    page_numbers: Iterable[int],
    *,
    dpi: int = 144,
) -> FlattenReport:
    """Flatten selected one-based pages to opaque RGB using only owned code.

    The page is rendered with ``rotate=0`` so the embedded raster represents the
    original page user space. The existing page-tree /Rotate value is left
    untouched, preventing a second rotation when the flattened candidate is
    displayed.
    """
    if dpi <= 0 or dpi > 2400:
        raise ValueError("dpi must be between 1 and 2400")

    requested = sorted(set(int(value) for value in page_numbers))
    if any(value <= 0 for value in requested):
        raise ValueError("page numbers are one-based positive integers")
    if not requested:
        return FlattenReport(())

    pages = list(walk_pages(doc))
    if requested[-1] > len(pages):
        raise TransparencyFlattenError(
            f"requested page {requested[-1]} but document has {len(pages)} page(s)"
        )

    flattened: list[FlattenedPage] = []
    for page_number in requested:
        page = pages[page_number - 1]
        try:
            rendered = TransparencyRenderer(doc, dpi=dpi).render_page(_unrotated(page))
        except (UnsupportedRenderingError, RenderingError, ValueError) as exc:
            raise TransparencyFlattenError(
                f"page {page_number} cannot be flattened by the owned renderer: {exc}"
            ) from exc

        resource_name = f"PDF2PDFAFlatten{page_number}"
        image_ref = doc.new_object(
            _image_stream(rendered.rgb_bytes(), rendered.width, rendered.height)
        )
        content_ref = doc.new_object(
            PDFStream(PDFDict(), _replacement_content(page, resource_name))
        )

        # The old page resources are no longer needed by page painting. Making
        # the replacement resources direct also prevents mutation of an
        # inherited/shared resource dictionary used by sibling pages.
        page.dictionary["Resources"] = PDFDict(
            {"XObject": PDFDict({resource_name: image_ref})}
        )
        page.dictionary["Contents"] = content_ref

        # A page-level transparency group is now obsolete because the new page
        # painting content contains one opaque image only.
        group = page.dictionary.get("Group")
        if isinstance(group, PDFDict):
            subtype = group.get("S")
            if isinstance(subtype, PDFName) and subtype.value == "Transparency":
                page.dictionary.pop("Group", None)

        flattened.append(
            FlattenedPage(
                page_number=page_number,
                width=rendered.width,
                height=rendered.height,
                dpi=dpi,
            )
        )

    return FlattenReport(tuple(flattened))
