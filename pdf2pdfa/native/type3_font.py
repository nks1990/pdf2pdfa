"""Owned Type 3 font dictionary/encoding interpreter.

Type 3 glyphs are PDF content streams rather than external outline programs.
This module resolves the font dictionary, code-to-glyph-name encoding, widths,
FontMatrix, resources and CharProcs without delegating to a font library.

Non-ASCII named base encodings are accepted only when the code is explicitly
covered by /Differences; guessing glyph names would make visual fidelity unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .raster import Matrix
from .structure import resolve


class Type3FontError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Type3GlyphItem:
    raw_code: bytes
    char_name: str
    width_1000: float
    advance_x: float
    advance_y: float
    word_space: bool = False


_DIGITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"
)
_PUNCTUATION = {
    32: "space",
    33: "exclam",
    34: "quotedbl",
    35: "numbersign",
    36: "dollar",
    37: "percent",
    38: "ampersand",
    39: "quotesingle",
    40: "parenleft",
    41: "parenright",
    42: "asterisk",
    43: "plus",
    44: "comma",
    45: "hyphen",
    46: "period",
    47: "slash",
    58: "colon",
    59: "semicolon",
    60: "less",
    61: "equal",
    62: "greater",
    63: "question",
    64: "at",
    91: "bracketleft",
    92: "backslash",
    93: "bracketright",
    94: "asciicircum",
    95: "underscore",
    96: "grave",
    123: "braceleft",
    124: "bar",
    125: "braceright",
    126: "asciitilde",
}


def _ascii_glyph_names() -> dict[int, str]:
    result = dict(_PUNCTUATION)
    for index, name in enumerate(_DIGITS, start=48):
        result[index] = name
    for code in range(ord("A"), ord("Z") + 1):
        result[code] = chr(code)
    for code in range(ord("a"), ord("z") + 1):
        result[code] = chr(code)
    return result


_ASCII_NAMES = _ascii_glyph_names()
_SUPPORTED_BASE_ENCODINGS = {"StandardEncoding", "WinAnsiEncoding", "MacRomanEncoding"}


def _number(doc: PDFDocument, value: PDFObject | None, label: str) -> float:
    if value is None:
        raise Type3FontError(f"{label} is missing")
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise Type3FontError(f"{label} is not numeric")
    return float(value)


def _integer(doc: PDFDocument, value: PDFObject | None, label: str) -> int:
    number = _number(doc, value, label)
    integer = int(number)
    if integer != number:
        raise Type3FontError(f"{label} is not an integer")
    return integer


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    if value is None:
        raise Type3FontError(f"{label} is missing")
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise Type3FontError(f"{label} is not a dictionary")
    return value


def _array(doc: PDFDocument, value: PDFObject | None, label: str) -> list[PDFObject]:
    if value is None:
        raise Type3FontError(f"{label} is missing")
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise Type3FontError(f"{label} is not an array")
    return value


def _matrix(doc: PDFDocument, value: PDFObject | None) -> Matrix:
    values = _array(doc, value, "Type3 /FontMatrix")
    if len(values) != 6:
        raise Type3FontError("Type3 /FontMatrix shall contain six numbers")
    numbers = [_number(doc, item, "Type3 /FontMatrix") for item in values]
    matrix = Matrix(*numbers)
    determinant = matrix.a * matrix.d - matrix.b * matrix.c
    if abs(determinant) < 1e-15:
        raise Type3FontError("Type3 /FontMatrix is singular")
    return matrix


def _base_encoding(name: str | None) -> dict[int, str]:
    if name is None:
        return {}
    if name not in _SUPPORTED_BASE_ENCODINGS:
        raise Type3FontError(f"unsupported Type3 base encoding /{name}")
    return dict(_ASCII_NAMES)


def _encoding(doc: PDFDocument, value: PDFObject | None) -> dict[int, str]:
    if value is None:
        raise Type3FontError("Type3 /Encoding is missing")
    value = resolve(doc, value)
    if isinstance(value, PDFName):
        return _base_encoding(value.value)
    if not isinstance(value, PDFDict):
        raise Type3FontError("Type3 /Encoding is neither a name nor dictionary")

    base_value = resolve(doc, value.get("BaseEncoding")) if value.get("BaseEncoding") is not None else None
    base_name: str | None
    if base_value is None:
        base_name = None
    elif isinstance(base_value, PDFName):
        base_name = base_value.value
    else:
        raise Type3FontError("Type3 Encoding /BaseEncoding is not a name")
    mapping = _base_encoding(base_name)

    differences_value = resolve(doc, value.get("Differences")) if value.get("Differences") is not None else None
    if differences_value is None:
        return mapping
    if not isinstance(differences_value, list):
        raise Type3FontError("Type3 Encoding /Differences is not an array")

    current: int | None = None
    for item in differences_value:
        item = resolve(doc, item)
        if isinstance(item, bool):
            raise Type3FontError("boolean is not valid in Type3 /Differences")
        if isinstance(item, (int, Decimal)):
            integer = int(item)
            if integer != item or not 0 <= integer <= 255:
                raise Type3FontError("Type3 /Differences code shall be an integer 0..255")
            current = integer
            continue
        if not isinstance(item, PDFName) or current is None:
            raise Type3FontError("malformed Type3 /Differences array")
        if current > 255:
            raise Type3FontError("Type3 /Differences runs past character code 255")
        mapping[current] = item.value
        current += 1
    return mapping


class Type3TextFont:
    """Resolve a Type 3 font into glyph procedure names and text advances."""

    is_type3 = True

    def __init__(self, doc: PDFDocument, font_value: PDFObject) -> None:
        self.doc = doc
        self.font = _dict(doc, font_value, "Type3 font resource")
        subtype = resolve(doc, self.font.get("Subtype"))
        if not isinstance(subtype, PDFName) or subtype.value != "Type3":
            raise Type3FontError("font resource is not /Subtype /Type3")

        self.font_matrix = _matrix(doc, self.font.get("FontMatrix"))
        # Widths are horizontal character-space displacement vectors (w, 0).
        # FontMatrix maps that vector into text space. A vector with a Y
        # component is therefore valid and must advance the text matrix in 2D.
        if abs(self.font_matrix.a) < 1e-15 and abs(self.font_matrix.b) < 1e-15:
            raise Type3FontError("Type3 FontMatrix collapses the horizontal advance vector")

        self.encoding = _encoding(doc, self.font.get("Encoding"))
        self.char_procs = _dict(doc, self.font.get("CharProcs"), "Type3 /CharProcs")
        resources_value = self.font.get("Resources")
        if resources_value is None:
            self.resources: PDFDict | None = None
        else:
            resources = resolve(doc, resources_value)
            if not isinstance(resources, PDFDict):
                raise Type3FontError("Type3 /Resources is not a dictionary")
            self.resources = resources

        self.first_char = _integer(doc, self.font.get("FirstChar"), "Type3 /FirstChar")
        self.last_char = _integer(doc, self.font.get("LastChar"), "Type3 /LastChar")
        if not 0 <= self.first_char <= self.last_char <= 255:
            raise Type3FontError("Type3 FirstChar/LastChar range is invalid")
        raw_widths = _array(doc, self.font.get("Widths"), "Type3 /Widths")
        expected = self.last_char - self.first_char + 1
        if len(raw_widths) != expected:
            raise Type3FontError(
                f"Type3 /Widths has {len(raw_widths)} entries, expected {expected}"
            )
        self.widths = [
            _number(doc, value, f"Type3 /Widths[{index}]")
            for index, value in enumerate(raw_widths)
        ]

    def decode(self, data: bytes) -> list[Type3GlyphItem]:
        result: list[Type3GlyphItem] = []
        for code in data:
            name = self.encoding.get(code)
            if name is None:
                if ".notdef" in self.char_procs:
                    name = ".notdef"
                else:
                    raise Type3FontError(
                        f"Type3 character code {code} has no owned encoding mapping or .notdef"
                    )
            if name not in self.char_procs:
                if ".notdef" in self.char_procs:
                    name = ".notdef"
                else:
                    raise Type3FontError(f"Type3 /CharProcs has no /{name}")
            if not self.first_char <= code <= self.last_char:
                width = 0.0
            else:
                width = self.widths[code - self.first_char]

            advance_x = width * self.font_matrix.a
            advance_y = width * self.font_matrix.b
            result.append(
                Type3GlyphItem(
                    raw_code=bytes([code]),
                    char_name=name,
                    width_1000=advance_x * 1000.0,
                    advance_x=advance_x,
                    advance_y=advance_y,
                    word_space=code == 32,
                )
            )
        return result

    def charproc(self, name: str) -> PDFStream:
        if name not in self.char_procs:
            raise Type3FontError(f"Type3 /CharProcs has no /{name}")
        value = resolve(self.doc, self.char_procs[name])
        if not isinstance(value, PDFStream):
            raise Type3FontError(f"Type3 /CharProcs /{name} is not a stream")
        return value
