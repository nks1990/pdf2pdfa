"""Deterministic classic-xref PDF writer owned by pdf2pdfa."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from .document import PDFDocument, PDFParseError
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .tokenizer import encode_name


class PDFWriteError(ValueError):
    pass


def _format_decimal(value: Decimal) -> bytes:
    if not value.is_finite():
        raise PDFWriteError("PDF real numbers must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0", "+0"):
        text = "0"
    return text.encode("ascii")


def serialize_object(value: PDFObject) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, bytes):
        # Hex strings avoid encoding ambiguities and preserve every byte.
        return b"<" + value.hex().upper().encode("ascii") + b">"
    if isinstance(value, PDFName):
        return encode_name(value)
    if isinstance(value, PDFRef):
        return f"{value.object_number} {value.generation} R".encode("ascii")
    if isinstance(value, list):
        return b"[" + b" ".join(serialize_object(item) for item in value) + b"]"
    if isinstance(value, PDFDict):
        parts: list[bytes] = []
        for key in sorted(value, key=lambda item: encode_name(item)):
            parts.append(encode_name(key))
            parts.append(serialize_object(value[key]))
        if not parts:
            return b"<<>>"
        return b"<<\n" + b" ".join(parts) + b"\n>>"
    if isinstance(value, PDFStream):
        dictionary = PDFDict(value.dictionary)
        dictionary["Length"] = len(value.data)
        return (
            serialize_object(dictionary)
            + b"\nstream\n"
            + value.data
            + b"\nendstream"
        )
    raise PDFWriteError(f"Unsupported PDF object type: {type(value).__name__}")


class PDFWriter:
    """Serialize a `PDFDocument` without qpdf/pikepdf/Ghostscript.

    Output always uses a classic xref table and emits object-stream members as
    normal indirect objects.  This representation is valid for all currently
    supported pdf2pdfa targets and is deliberately chosen to keep PDF/A-1
    output free of PDF 1.5 cross-reference/object-stream features.
    """

    def __init__(
        self,
        document: PDFDocument,
        *,
        version: str | None = None,
        reachable_only: bool = True,
    ) -> None:
        self.document = document
        self.version = version or document.header_version
        self.reachable_only = reachable_only
        if self.version not in {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7"}:
            raise PDFWriteError(f"Unsupported output PDF version {self.version!r}")

    def to_bytes(self) -> bytes:
        output = BytesIO()
        self.write(output)
        return output.getvalue()

    def write(self, destination: str | Path | BinaryIO) -> None:
        close = False
        if isinstance(destination, (str, Path)):
            stream: BinaryIO = Path(destination).open("wb")
            close = True
        else:
            stream = destination
        try:
            self._write_stream(stream)
        finally:
            if close:
                stream.close()

    def _objects(self) -> list[tuple[PDFRef, PDFObject]]:
        if not self.reachable_only:
            return list(self.document.iter_indirect_objects())
        reachable = self.document.reachable_refs()
        objects: list[tuple[PDFRef, PDFObject]] = []
        for ref in sorted(reachable, key=lambda item: (item.object_number, item.generation)):
            try:
                value = self.document.get(ref)
            except KeyError as exc:
                raise PDFWriteError(f"Reachable reference {ref} is missing") from exc
            # Infrastructure streams from the source xref/object-stream layout
            # are not logical document objects and must not be copied.
            if isinstance(value, PDFStream):
                obj_type = value.get("Type")
                if isinstance(obj_type, PDFName) and obj_type.value in ("XRef", "ObjStm"):
                    continue
            objects.append((ref, value))
        return objects

    def _write_stream(self, output: BinaryIO) -> None:
        output.write(f"%PDF-{self.version}\n".encode("ascii"))
        # Binary marker recommended by the PDF specification.
        output.write(b"%\xe2\xe3\xcf\xd3\n")

        objects = self._objects()
        if not any(ref == self.document.root_ref for ref, _ in objects):
            # `reachable_refs` always begins at Root; this protects custom
            # writer use when reachable_only=False or a malformed overlay exists.
            try:
                objects.append((self.document.root_ref, self.document.get(self.document.root_ref)))
            except Exception as exc:
                raise PDFWriteError("Document root cannot be serialized") from exc
        objects.sort(key=lambda item: (item[0].object_number, item[0].generation))

        offsets: dict[int, tuple[int, int]] = {}
        for ref, value in objects:
            if ref.object_number in offsets:
                raise PDFWriteError(f"Multiple live generations for object {ref.object_number}")
            offset = output.tell()
            if offset >= 10_000_000_000:
                raise PDFWriteError("Classic xref output exceeds the 10-digit offset limit")
            offsets[ref.object_number] = (offset, ref.generation)
            output.write(f"{ref.object_number} {ref.generation} obj\n".encode("ascii"))
            output.write(serialize_object(value))
            output.write(b"\nendobj\n")

        highest = max([0, *offsets.keys(), self.document.root_ref.object_number])
        size = highest + 1
        xref_offset = output.tell()
        output.write(f"xref\n0 {size}\n".encode("ascii"))
        output.write(b"0000000000 65535 f \n")
        for number in range(1, size):
            entry = offsets.get(number)
            if entry is None:
                output.write(b"0000000000 00000 f \n")
            else:
                offset, generation = entry
                if generation > 99999:
                    raise PDFWriteError("PDF generation number exceeds xref field width")
                output.write(f"{offset:010d} {generation:05d} n \n".encode("ascii"))

        trailer = PDFDict()
        for key, value in self.document.trailer.items():
            if key in {"Size", "Prev", "XRefStm", "Encrypt", "Type", "W", "Index", "Length", "Filter", "DecodeParms"}:
                continue
            trailer[key] = value
        trailer["Size"] = size
        trailer["Root"] = self.document.root_ref
        output.write(b"trailer\n")
        output.write(serialize_object(trailer))
        output.write(b"\nstartxref\n")
        output.write(str(xref_offset).encode("ascii"))
        output.write(b"\n%%EOF\n")
