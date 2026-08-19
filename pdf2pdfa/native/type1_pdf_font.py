"""PDF `/Type1 + FontFile` adapter for the owned Type 1 core."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .raster import Matrix, Path
from .structure import decoded_stream_bytes, resolve
from .type1 import Type1Error, Type1Font, UnsupportedType1Error
from .type1_encoding import (
    Type1EncodingError,
    parse_type1_builtin_encoding,
    parse_type1_pdf_encoding,
)


class Type1PDFFontError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Type1GlyphItem:
    raw_code: bytes
    glyph_name: str
    width_1000: float
    word_space: bool = False


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise Type1PDFFontError(f"{label} is not a dictionary")
    return value


def _name(doc: PDFDocument, value: PDFObject | None) -> str:
    value = resolve(doc, value) if value is not None else None
    return value.value if isinstance(value, PDFName) else ""


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    if value is None:
        return default
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return default
    return float(value)


def _widths(doc: PDFDocument, font: PDFDict) -> tuple[int, list[float], float]:
    first = int(_number(doc, font.get("FirstChar"), 0))
    last = int(_number(doc, font.get("LastChar"), 255))
    if not 0 <= first <= last <= 255:
        raise Type1PDFFontError("Type1 FirstChar/LastChar range is invalid")
    raw = resolve(doc, font.get("Widths")) if font.get("Widths") is not None else None
    if not isinstance(raw, list):
        raise Type1PDFFontError("embedded PDF Type1 font requires a Widths array")
    expected = last - first + 1
    if len(raw) != expected:
        raise Type1PDFFontError(
            f"Type1 Widths has {len(raw)} entries, expected {expected}"
        )
    widths = [_number(doc, item, 0.0) for item in raw]
    descriptor = _dict(doc, font.get("FontDescriptor"), "FontDescriptor")
    missing = _number(doc, descriptor.get("MissingWidth"), 0.0)
    return first, widths, missing


def _program(doc: PDFDocument, descriptor: PDFDict) -> tuple[Type1Font, bytes]:
    value = resolve(doc, descriptor.get("FontFile")) if descriptor.get("FontFile") is not None else None
    if not isinstance(value, PDFStream):
        raise Type1PDFFontError("Type1 font program is not embedded in FontDescriptor /FontFile")
    try:
        data = decoded_stream_bytes(doc, value, label="Type1 FontFile")
        return Type1Font(data), data
    except (Type1Error, UnsupportedType1Error) as exc:
        raise Type1PDFFontError(f"embedded Type1 program is invalid/unsupported: {exc}") from exc


def _matrix(values: tuple[float, ...]) -> Matrix:
    if len(values) != 6:
        raise Type1PDFFontError("Type1 FontMatrix shall contain six numbers")
    matrix = Matrix(*values)
    if abs(matrix.a * matrix.d - matrix.b * matrix.c) <= 1e-18:
        raise Type1PDFFontError("Type1 FontMatrix is singular")
    return matrix


class Type1PDFTextFont:
    """Resolve simple PDF Type1 character codes to original embedded outlines."""

    is_type3 = False
    is_cff = False
    is_type1 = True

    def __init__(self, doc: PDFDocument, font_value: PDFObject) -> None:
        self.doc = doc
        self.font = _dict(doc, font_value, "font resource")
        subtype = _name(doc, self.font.get("Subtype"))
        if subtype != "Type1":
            raise Type1PDFFontError(f"expected PDF /Type1 font, got /{subtype or 'unknown'}")
        self.base_font = _name(doc, self.font.get("BaseFont"))
        descriptor = _dict(doc, self.font.get("FontDescriptor"), "FontDescriptor")
        self.program, program_bytes = _program(doc, descriptor)
        try:
            built_in = parse_type1_builtin_encoding(program_bytes)
            self.encoding = parse_type1_pdf_encoding(
                doc,
                self.font.get("Encoding"),
                built_in=built_in,
            )
        except Type1EncodingError as exc:
            raise Type1PDFFontError(str(exc)) from exc
        self.first_char, self.widths, self.missing_width = _widths(doc, self.font)

    def decode(self, data: bytes) -> list[Type1GlyphItem]:
        result: list[Type1GlyphItem] = []
        for code in data:
            name = self.encoding.get(code)
            if name is None:
                raise Type1PDFFontError(
                    f"Type1 character code {code} has no owned encoding mapping"
                )
            glyph_name = name if self.program.has_glyph(name) else ".notdef"
            index = code - self.first_char
            width = self.widths[index] if 0 <= index < len(self.widths) else self.missing_width
            result.append(
                Type1GlyphItem(
                    raw_code=bytes([code]),
                    glyph_name=glyph_name,
                    width_1000=width,
                    word_space=code == 32,
                )
            )
        return result

    @property
    def glyph_matrix(self) -> Matrix:
        return _matrix(self.program.font_matrix)

    def glyph_path(self, name: str, transform: Matrix) -> Path:
        try:
            outline = self.program.outline(name)
        except (Type1Error, UnsupportedType1Error) as exc:
            raise Type1PDFFontError(f"Type1 glyph /{name} cannot be interpreted: {exc}") from exc
        matrix = transform.concat(self.glyph_matrix)
        path = Path()
        for command in outline.commands:
            if command.operator == "M":
                path.move_to(*matrix.transform(command.values[0], command.values[1]))
            elif command.operator == "L":
                path.line_to(*matrix.transform(command.values[0], command.values[1]))
            elif command.operator == "C":
                p1 = matrix.transform(command.values[0], command.values[1])
                p2 = matrix.transform(command.values[2], command.values[3])
                p3 = matrix.transform(command.values[4], command.values[5])
                path.curve_to(*p1, *p2, *p3, tolerance=0.25)
            elif command.operator == "Z":
                path.close()
            else:
                raise Type1PDFFontError(
                    f"unsupported Type1 outline command {command.operator!r}"
                )
        return path
