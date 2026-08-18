"""Owned PDF/A-1 annotation-appearance transparency flattening.

When a page contains transparent annotation appearances, the static page view
is rendered with current normal appearances included and becomes one opaque RGB
page image. Every appearance stream on that page is then replaced by an empty
Form XObject that preserves BBox/Matrix and AP state structure. This prevents a
second visual paint while retaining the annotation dictionary, Rect, subtype,
AS selection and non-appearance semantics.

The operation is intentionally archival/static: rollover/down visual states are
neutralized together with normal states because PDF/A-1 may not retain their
transparent painting programs. The normal visual state is preserved in page
content and checked by the annotation-aware visual fidelity gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .document import PDFDocument
from .flatten import (
    FlattenedPage,
    TransparencyFlattenError,
    _image_stream,
    _replacement_content,
    _unrotated,
)
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .owned_renderer import FullOwnedPageRenderer
from .page_render import RenderingError, UnsupportedRenderingError
from .structure import resolve, walk_pages


@dataclass(frozen=True, slots=True)
class AnnotationFlattenReport:
    pages: tuple[FlattenedPage, ...]
    neutralized_appearances: int

    @property
    def count(self) -> int:
        return len(self.pages)


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise TransparencyFlattenError(f"{label} is not a dictionary")
    return value


def _stream(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFStream:
    value = resolve(doc, value)
    if not isinstance(value, PDFStream):
        raise TransparencyFlattenError(f"{label} is not a stream")
    return value


def _empty_appearance(source: PDFStream) -> PDFStream:
    if source.get("BBox") is None:
        raise TransparencyFlattenError("annotation appearance stream has no /BBox")
    dictionary = PDFDict(
        {
            "Type": PDFName("XObject"),
            "Subtype": PDFName("Form"),
            "BBox": source.get("BBox"),
            "Resources": PDFDict(),
        }
    )
    if source.get("Matrix") is not None:
        dictionary["Matrix"] = source.get("Matrix")
    if source.get("FormType") is not None:
        dictionary["FormType"] = source.get("FormType")
    return PDFStream(dictionary, b"")


def _replace_ap_value(
    doc: PDFDocument,
    value: PDFObject,
    *,
    label: str,
) -> tuple[PDFObject, int]:
    resolved = resolve(doc, value)
    if isinstance(resolved, PDFStream):
        return doc.new_object(_empty_appearance(resolved)), 1
    if not isinstance(resolved, PDFDict):
        raise TransparencyFlattenError(
            f"{label} is neither appearance stream nor appearance-state dictionary"
        )
    replacement = PDFDict()
    count = 0
    for state, state_value in resolved.items():
        stream = _stream(doc, state_value, f"{label}/{state}")
        replacement[state] = doc.new_object(_empty_appearance(stream))
        count += 1
    if not replacement:
        raise TransparencyFlattenError(f"{label} appearance-state dictionary is empty")
    return replacement, count


def _neutralize_page_appearances(doc: PDFDocument, page, page_number: int) -> int:
    raw_annots = resolve(doc, page.dictionary.get("Annots")) if page.dictionary.get("Annots") is not None else None
    if raw_annots is None:
        return 0
    if not isinstance(raw_annots, list):
        raise TransparencyFlattenError(f"page {page_number} /Annots is not an array")
    total = 0
    for annot_index, annot_value in enumerate(raw_annots, start=1):
        annot = _dict(doc, annot_value, f"page {page_number} annotation {annot_index}")
        raw_ap = resolve(doc, annot.get("AP")) if annot.get("AP") is not None else None
        if raw_ap is None:
            continue
        if not isinstance(raw_ap, PDFDict):
            raise TransparencyFlattenError(
                f"page {page_number} annotation {annot_index} /AP is not a dictionary"
            )
        replacement_ap = PDFDict()
        for key, value in raw_ap.items():
            if key not in {"N", "R", "D"}:
                # Unknown appearance-dictionary entries are not copied because
                # they may retain painting dependencies outside the static AP
                # model. Refuse rather than silently discard them.
                raise TransparencyFlattenError(
                    f"page {page_number} annotation {annot_index} /AP has unsupported /{key} entry"
                )
            replacement, count = _replace_ap_value(
                doc,
                value,
                label=f"page {page_number} annotation {annot_index} /AP/{key}",
            )
            replacement_ap[key] = replacement
            total += count
        annot["AP"] = replacement_ap
    return total


def flatten_pages_with_annotations(
    doc: PDFDocument,
    page_numbers: Iterable[int],
    *,
    dpi: int = 144,
) -> AnnotationFlattenReport:
    """Bake current normal annotation visuals into selected opaque page rasters."""
    if dpi <= 0 or dpi > 2400:
        raise ValueError("dpi must be between 1 and 2400")
    requested = sorted(set(int(value) for value in page_numbers))
    if any(value <= 0 for value in requested):
        raise ValueError("page numbers are one-based positive integers")
    if not requested:
        return AnnotationFlattenReport((), 0)

    pages = list(walk_pages(doc))
    if requested[-1] > len(pages):
        raise TransparencyFlattenError(
            f"requested page {requested[-1]} but document has {len(pages)} page(s)"
        )

    flattened: list[FlattenedPage] = []
    total_neutralized = 0
    for page_number in requested:
        page = pages[page_number - 1]
        try:
            rendered = FullOwnedPageRenderer(
                doc,
                dpi=dpi,
                render_annotations=True,
            ).render_page(_unrotated(page))
        except (UnsupportedRenderingError, RenderingError, ValueError) as exc:
            raise TransparencyFlattenError(
                f"page {page_number} with annotations cannot be flattened by the owned renderer: {exc}"
            ) from exc

        total_neutralized += _neutralize_page_appearances(doc, page, page_number)

        resource_name = f"PDF2PDFAAnnotationFlatten{page_number}"
        image_ref = doc.new_object(
            _image_stream(rendered.rgb_bytes(), rendered.width, rendered.height)
        )
        content_ref = doc.new_object(
            PDFStream(PDFDict(), _replacement_content(page, resource_name))
        )
        page.dictionary["Resources"] = PDFDict(
            {"XObject": PDFDict({resource_name: image_ref})}
        )
        page.dictionary["Contents"] = content_ref

        group = resolve(doc, page.dictionary.get("Group")) if page.dictionary.get("Group") is not None else None
        if isinstance(group, PDFDict):
            subtype = resolve(doc, group.get("S")) if group.get("S") is not None else None
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

    return AnnotationFlattenReport(tuple(flattened), total_neutralized)
