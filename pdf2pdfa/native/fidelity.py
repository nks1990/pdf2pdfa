"""Owned semantic-fidelity verification for conservative PDF/A repairs.

Until the native renderer covers every PDF painting feature, the publication
contract for structural repairs is stronger and simpler: page painting
instructions, geometry, text-show operations and image payloads must remain
semantically unchanged.  Metadata/security/color-characterization repairs may
change document structure around those invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Iterable

from .content import ContentInstruction, InlineImage, parse_content_stream
from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .structure import (
    decoded_stream_bytes,
    page_content_bytes,
    resolve,
    walk_pages,
    walk_reachable_objects,
)


class FidelityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, order=True)
class ImageFingerprint:
    width: int
    height: int
    bits_per_component: int
    color_space: str
    filters: tuple[str, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True, order=True)
class AttachmentFingerprint:
    filename: str
    mime: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class PageFingerprint:
    media_box: tuple[str, str, str, str]
    crop_box: tuple[str, str, str, str]
    rotate: int
    content_sha256: str
    text_show_sha256: str
    inline_images: tuple[ImageFingerprint, ...]


@dataclass(frozen=True, slots=True)
class DocumentFingerprint:
    pages: tuple[PageFingerprint, ...]
    images: tuple[ImageFingerprint, ...]
    attachments: tuple[AttachmentFingerprint, ...]


@dataclass(frozen=True, slots=True)
class FidelityDifference:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SemanticFidelityReport:
    passed: bool
    differences: tuple[FidelityDifference, ...]
    source: DocumentFingerprint
    candidate: DocumentFingerprint
    engine: str = "pdf2pdfa-owned-semantic"


def _number(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _stream(doc: PDFDocument, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _filters(doc: PDFDocument, stream: PDFStream) -> tuple[str, ...]:
    try:
        value = resolve(doc, stream.get("Filter"))
    except Exception:
        return ("<unresolvable>",)
    if value is None:
        return ()
    if isinstance(value, PDFName):
        return (value.value,)
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            try:
                item = resolve(doc, item)
            except Exception:
                names.append("<unresolvable>")
                continue
            names.append(item.value if isinstance(item, PDFName) else "<invalid>")
        return tuple(names)
    return ("<invalid>",)


def _color_family(doc: PDFDocument, value: PDFObject | None) -> str:
    try:
        value = resolve(doc, value)
    except Exception:
        return "<unresolvable>"
    if value is None:
        return ""
    if isinstance(value, PDFName):
        return value.value
    if isinstance(value, list) and value:
        try:
            first = resolve(doc, value[0])
        except Exception:
            return "<unresolvable>"
        if isinstance(first, PDFName):
            return first.value
    return "<complex>"


def _image_payload(doc: PDFDocument, stream: PDFStream) -> bytes:
    filters = set(_filters(doc, stream))
    terminal = {"DCTDecode", "DCT", "JPXDecode", "JBIG2Decode", "CCITTFaxDecode", "CCF"}
    if filters & terminal:
        # The repair path does not transcode terminal image codecs until the
        # native image engine is involved. Hashing encoded bytes therefore
        # proves preservation without requiring a decoder here.
        return stream.data
    try:
        return decoded_stream_bytes(doc, stream)
    except Exception:
        return stream.data


def _image_fingerprint(doc: PDFDocument, stream: PDFStream) -> ImageFingerprint:
    width = resolve(doc, stream.get("Width")) if stream.get("Width") is not None else 0
    height = resolve(doc, stream.get("Height")) if stream.get("Height") is not None else 0
    bpc = resolve(doc, stream.get("BitsPerComponent")) if stream.get("BitsPerComponent") is not None else 0
    return ImageFingerprint(
        width=int(width) if isinstance(width, int) and not isinstance(width, bool) else 0,
        height=int(height) if isinstance(height, int) and not isinstance(height, bool) else 0,
        bits_per_component=int(bpc) if isinstance(bpc, int) and not isinstance(bpc, bool) else 0,
        color_space=_color_family(doc, stream.get("ColorSpace")),
        filters=_filters(doc, stream),
        payload_sha256=_sha(_image_payload(doc, stream)),
    )


def _inline_image_fingerprint(item: InlineImage) -> ImageFingerprint:
    width = item.dictionary.get("W", item.dictionary.get("Width", 0))
    height = item.dictionary.get("H", item.dictionary.get("Height", 0))
    bpc = item.dictionary.get("BPC", item.dictionary.get("BitsPerComponent", 0))
    cs = item.dictionary.get("CS", item.dictionary.get("ColorSpace"))
    filt = item.dictionary.get("F", item.dictionary.get("Filter"))
    if isinstance(filt, PDFName):
        filters = (filt.value,)
    elif isinstance(filt, list):
        filters = tuple(value.value for value in filt if isinstance(value, PDFName))
    else:
        filters = ()
    return ImageFingerprint(
        width=width if isinstance(width, int) and not isinstance(width, bool) else 0,
        height=height if isinstance(height, int) and not isinstance(height, bool) else 0,
        bits_per_component=bpc if isinstance(bpc, int) and not isinstance(bpc, bool) else 0,
        color_space=_name(cs),
        filters=filters,
        payload_sha256=_sha(item.data),
    )


def _text_semantics(items: Iterable[ContentInstruction | InlineImage]) -> bytes:
    output = bytearray()
    selected_font = b""
    for item in items:
        if not isinstance(item, ContentInstruction):
            continue
        if item.operator == "Tf" and item.operands and isinstance(item.operands[0], PDFName):
            selected_font = item.operands[0].value.encode("latin-1")
            output.extend(b"Tf:")
            output.extend(selected_font)
            output.extend(b"\n")
            continue
        if item.operator not in ("Tj", "TJ", "'", '"'):
            continue
        output.extend(item.operator.encode("ascii"))
        output.extend(b"|")
        output.extend(selected_font)
        output.extend(b"|")
        for operand in item.operands:
            if isinstance(operand, bytes):
                output.extend(b"s")
                output.extend(len(operand).to_bytes(8, "big"))
                output.extend(operand)
            elif isinstance(operand, list):
                output.extend(b"[")
                for element in operand:
                    if isinstance(element, bytes):
                        output.extend(b"s")
                        output.extend(len(element).to_bytes(8, "big"))
                        output.extend(element)
                    elif isinstance(element, (int, Decimal)) and not isinstance(element, bool):
                        output.extend(b"n")
                        output.extend(str(element).encode("ascii"))
                    else:
                        output.extend(repr(element).encode("ascii", "backslashreplace"))
                output.extend(b"]")
            elif isinstance(operand, (int, Decimal)) and not isinstance(operand, bool):
                output.extend(b"n")
                output.extend(str(operand).encode("ascii"))
            else:
                output.extend(repr(operand).encode("ascii", "backslashreplace"))
        output.extend(b"\n")
    return bytes(output)


def _attachment_filename(doc: PDFDocument, spec: PDFDict) -> str:
    for key in ("UF", "F"):
        try:
            value = resolve(doc, spec.get(key))
        except Exception:
            continue
        if isinstance(value, bytes):
            if value.startswith(b"\xfe\xff"):
                try:
                    return value[2:].decode("utf-16-be")
                except UnicodeDecodeError:
                    pass
            return value.decode("latin-1", "replace")
    return ""


def _attachment_fingerprints(doc: PDFDocument) -> tuple[AttachmentFingerprint, ...]:
    result: list[AttachmentFingerprint] = []
    seen: set[str] = set()
    for path, value in walk_reachable_objects(doc):
        spec = value if isinstance(value, PDFDict) else None
        if not spec or _name(spec.get("Type")) != "Filespec" or spec.get("EF") is None:
            continue
        identity = path
        if identity in seen:
            continue
        seen.add(identity)
        ef = _dict(doc, spec.get("EF"))
        if not ef:
            continue
        stream = _stream(doc, ef.get("UF")) or _stream(doc, ef.get("F"))
        if stream is None:
            continue
        try:
            payload = decoded_stream_bytes(doc, stream)
        except Exception:
            payload = stream.data
        result.append(
            AttachmentFingerprint(
                filename=_attachment_filename(doc, spec),
                mime=_name(resolve(doc, stream.get("Subtype"))) if stream.get("Subtype") is not None else "",
                payload_sha256=_sha(payload),
            )
        )
    return tuple(sorted(result))


def fingerprint(source: str | Path | bytes | PDFDocument) -> DocumentFingerprint:
    doc = source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=False)
    pages: list[PageFingerprint] = []
    for page in walk_pages(doc):
        content = page_content_bytes(doc, page)
        items = parse_content_stream(content)
        inline_images = tuple(
            _inline_image_fingerprint(item) for item in items if isinstance(item, InlineImage)
        )
        pages.append(
            PageFingerprint(
                media_box=tuple(_number(value) for value in page.media_box),  # type: ignore[arg-type]
                crop_box=tuple(_number(value) for value in page.crop_box),  # type: ignore[arg-type]
                rotate=page.rotate,
                content_sha256=_sha(content),
                text_show_sha256=_sha(_text_semantics(items)),
                inline_images=inline_images,
            )
        )

    images: list[ImageFingerprint] = []
    seen_streams: set[int] = set()
    for _path, value in walk_reachable_objects(doc):
        if not isinstance(value, PDFStream) or _name(value.get("Subtype")) != "Image":
            continue
        identity = id(value)
        if identity in seen_streams:
            continue
        seen_streams.add(identity)
        images.append(_image_fingerprint(doc, value))

    return DocumentFingerprint(
        pages=tuple(pages),
        images=tuple(sorted(images)),
        attachments=_attachment_fingerprints(doc),
    )


class NativeFidelityChecker:
    """Compare semantic preservation invariants using only owned code."""

    def compare(
        self,
        source: str | Path | bytes | PDFDocument,
        candidate: str | Path | bytes | PDFDocument,
        *,
        allow_attachment_changes: bool = False,
    ) -> SemanticFidelityReport:
        before = fingerprint(source)
        after = fingerprint(candidate)
        differences: list[FidelityDifference] = []

        if len(before.pages) != len(after.pages):
            differences.append(
                FidelityDifference(
                    "pages.count",
                    f"page count changed from {len(before.pages)} to {len(after.pages)}",
                )
            )
        for index, (left, right) in enumerate(zip(before.pages, after.pages), start=1):
            if left.media_box != right.media_box:
                differences.append(
                    FidelityDifference(
                        "pages.media_box",
                        f"page {index} MediaBox changed: {left.media_box} -> {right.media_box}",
                    )
                )
            if left.crop_box != right.crop_box:
                differences.append(
                    FidelityDifference(
                        "pages.crop_box",
                        f"page {index} CropBox changed: {left.crop_box} -> {right.crop_box}",
                    )
                )
            if left.rotate != right.rotate:
                differences.append(
                    FidelityDifference(
                        "pages.rotate",
                        f"page {index} rotation changed: {left.rotate} -> {right.rotate}",
                    )
                )
            if left.content_sha256 != right.content_sha256:
                differences.append(
                    FidelityDifference(
                        "pages.content",
                        f"page {index} decoded painting instruction bytes changed",
                    )
                )
            if left.text_show_sha256 != right.text_show_sha256:
                differences.append(
                    FidelityDifference(
                        "text.show_operations",
                        f"page {index} text-show sequence changed",
                    )
                )
            if left.inline_images != right.inline_images:
                differences.append(
                    FidelityDifference(
                        "images.inline",
                        f"page {index} inline-image inventory changed",
                    )
                )

        if before.images != after.images:
            differences.append(
                FidelityDifference(
                    "images.xobjects",
                    "image XObject inventory/payloads changed",
                )
            )
        if not allow_attachment_changes and before.attachments != after.attachments:
            differences.append(
                FidelityDifference(
                    "attachments.inventory",
                    "embedded-file inventory/payloads changed",
                )
            )

        return SemanticFidelityReport(
            passed=not differences,
            differences=tuple(differences),
            source=before,
            candidate=after,
        )
