"""Small owned PDF builder used by tests and native repair output.

The builder intentionally emits a simple classic-xref representation.  It is
not a second document model: complex mutations happen on ``PDFDocument``;
``PDFBuilder`` exists to construct deterministic new documents and fixtures
without relying on any third-party PDF library.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from .objects import PDFDict, PDFObject, PDFRef
from .writer import PDFWriteError, serialize_object


class PDFBuilder:
    def __init__(self, *, version: str = "1.4") -> None:
        if version not in {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7"}:
            raise ValueError(f"unsupported PDF version {version!r}")
        self.version = version
        self.objects: dict[PDFRef, PDFObject] = {}
        self.root_ref: PDFRef | None = None
        self.info_ref: PDFRef | None = None
        self._next_object = 1

    def add(self, value: PDFObject, *, generation: int = 0) -> PDFRef:
        while any(ref.object_number == self._next_object for ref in self.objects):
            self._next_object += 1
        ref = PDFRef(self._next_object, generation)
        self._next_object += 1
        self.objects[ref] = value
        return ref

    def put(self, ref: PDFRef, value: PDFObject) -> None:
        if ref in self.objects:
            raise ValueError(f"object {ref} already exists")
        if any(candidate.object_number == ref.object_number for candidate in self.objects):
            raise ValueError(f"object number {ref.object_number} already has a live generation")
        self.objects[ref] = value
        self._next_object = max(self._next_object, ref.object_number + 1)

    def set_root(self, ref: PDFRef) -> None:
        if ref not in self.objects:
            raise ValueError("root reference must exist in builder")
        self.root_ref = ref

    def set_info(self, ref: PDFRef | None) -> None:
        if ref is not None and ref not in self.objects:
            raise ValueError("info reference must exist in builder")
        self.info_ref = ref

    def to_bytes(self) -> bytes:
        buffer = BytesIO()
        self.write(buffer)
        return buffer.getvalue()

    def write(self, destination: str | Path | BinaryIO) -> None:
        if self.root_ref is None:
            raise PDFWriteError("PDFBuilder requires a document root")
        close = False
        if isinstance(destination, (str, Path)):
            output: BinaryIO = Path(destination).open("wb")
            close = True
        else:
            output = destination
        try:
            self._write(output)
        finally:
            if close:
                output.close()

    def _write(self, output: BinaryIO) -> None:
        assert self.root_ref is not None
        output.write(f"%PDF-{self.version}\n".encode("ascii"))
        output.write(b"%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, tuple[int, int]] = {}
        for ref in sorted(self.objects, key=lambda item: (item.object_number, item.generation)):
            if ref.object_number in offsets:
                raise PDFWriteError(f"multiple live generations for object {ref.object_number}")
            offset = output.tell()
            if offset >= 10_000_000_000:
                raise PDFWriteError("classic xref offset exceeds 10 digits")
            offsets[ref.object_number] = (offset, ref.generation)
            output.write(f"{ref.object_number} {ref.generation} obj\n".encode("ascii"))
            output.write(serialize_object(self.objects[ref]))
            output.write(b"\nendobj\n")

        size = max([self.root_ref.object_number, *offsets.keys()], default=0) + 1
        xref_offset = output.tell()
        output.write(f"xref\n0 {size}\n".encode("ascii"))
        output.write(b"0000000000 65535 f \n")
        for number in range(1, size):
            entry = offsets.get(number)
            if entry is None:
                output.write(b"0000000000 00000 f \n")
            else:
                offset, generation = entry
                output.write(f"{offset:010d} {generation:05d} n \n".encode("ascii"))

        trailer = PDFDict({"Size": size, "Root": self.root_ref})
        if self.info_ref is not None:
            trailer["Info"] = self.info_ref
        output.write(b"trailer\n")
        output.write(serialize_object(trailer))
        output.write(b"\nstartxref\n")
        output.write(str(xref_offset).encode("ascii"))
        output.write(b"\n%%EOF\n")
