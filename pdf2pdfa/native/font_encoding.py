"""Owned simple-font encoding maps for Type1/CFF PDF fonts.

The mapping result is a PostScript glyph name, not Unicode. PDF character
codes select encoding names and those names select embedded font CharStrings.

WinAnsi and Adobe StandardEncoding are implemented for all defined byte
positions. MacRoman intentionally remains conservative outside ASCII until its
complete owned table is added; callers fail closed instead of guessing.
"""

from __future__ import annotations

from decimal import Decimal

from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject
from .structure import resolve


class FontEncodingError(ValueError):
    pass


_DIGITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"
)
_PUNCTUATION = {
    32: "space", 33: "exclam", 34: "quotedbl", 35: "numbersign",
    36: "dollar", 37: "percent", 38: "ampersand", 39: "quotesingle",
    40: "parenleft", 41: "parenright", 42: "asterisk", 43: "plus",
    44: "comma", 45: "hyphen", 46: "period", 47: "slash",
    58: "colon", 59: "semicolon", 60: "less", 61: "equal",
    62: "greater", 63: "question", 64: "at", 91: "bracketleft",
    92: "backslash", 93: "bracketright", 94: "asciicircum",
    95: "underscore", 96: "grave", 123: "braceleft", 124: "bar",
    125: "braceright", 126: "asciitilde",
}


def _ascii_names() -> dict[int, str]:
    result = dict(_PUNCTUATION)
    for code, name in enumerate(_DIGITS, start=48):
        result[code] = name
    for code in range(65, 91):
        result[code] = chr(code)
    for code in range(97, 123):
        result[code] = chr(code)
    return result


_STANDARD_HIGH = {
    161: "exclamdown", 162: "cent", 163: "sterling", 164: "fraction",
    165: "yen", 166: "florin", 167: "section", 168: "currency",
    169: "quotesingle", 170: "quotedblleft", 171: "guillemotleft",
    172: "guilsinglleft", 173: "guilsinglright", 174: "fi", 175: "fl",
    177: "endash", 178: "dagger", 179: "daggerdbl", 180: "periodcentered",
    182: "paragraph", 183: "bullet", 184: "quotesinglbase",
    185: "quotedblbase", 186: "quotedblright", 187: "guillemotright",
    188: "ellipsis", 189: "perthousand", 191: "questiondown",
    193: "grave", 194: "acute", 195: "circumflex", 196: "tilde",
    197: "macron", 198: "breve", 199: "dotaccent", 200: "dieresis",
    202: "ring", 203: "cedilla", 205: "hungarumlaut", 206: "ogonek",
    207: "caron", 208: "emdash", 225: "AE", 227: "ordfeminine",
    232: "Lslash", 233: "Oslash", 234: "OE", 235: "ordmasculine",
    241: "ae", 245: "dotlessi", 248: "lslash", 249: "oslash",
    250: "oe", 251: "germandbls",
}


def _standard_names() -> dict[int, str]:
    result = _ascii_names()
    # Adobe StandardEncoding differs from ASCII/WinAnsi at these two positions.
    result[39] = "quoteright"
    result[96] = "quoteleft"
    result.update(_STANDARD_HIGH)
    return result


# PDF WinAnsiEncoding follows the Windows ANSI/CP1252 repertoire with Adobe
# glyph names. Undefined byte positions intentionally do not appear.
_WINANSI_HIGH = {
    0x80: "Euro", 0x82: "quotesinglbase", 0x83: "florin",
    0x84: "quotedblbase", 0x85: "ellipsis", 0x86: "dagger",
    0x87: "daggerdbl", 0x88: "circumflex", 0x89: "perthousand",
    0x8A: "Scaron", 0x8B: "guilsinglleft", 0x8C: "OE",
    0x8E: "Zcaron", 0x91: "quoteleft", 0x92: "quoteright",
    0x93: "quotedblleft", 0x94: "quotedblright", 0x95: "bullet",
    0x96: "endash", 0x97: "emdash", 0x98: "tilde",
    0x99: "trademark", 0x9A: "scaron", 0x9B: "guilsinglright",
    0x9C: "oe", 0x9E: "zcaron", 0x9F: "Ydieresis",
    0xA0: "space", 0xA1: "exclamdown", 0xA2: "cent",
    0xA3: "sterling", 0xA4: "currency", 0xA5: "yen",
    0xA6: "brokenbar", 0xA7: "section", 0xA8: "dieresis",
    0xA9: "copyright", 0xAA: "ordfeminine", 0xAB: "guillemotleft",
    0xAC: "logicalnot", 0xAD: "hyphen", 0xAE: "registered",
    0xAF: "macron", 0xB0: "degree", 0xB1: "plusminus",
    0xB2: "twosuperior", 0xB3: "threesuperior", 0xB4: "acute",
    0xB5: "mu", 0xB6: "paragraph", 0xB7: "periodcentered",
    0xB8: "cedilla", 0xB9: "onesuperior", 0xBA: "ordmasculine",
    0xBB: "guillemotright", 0xBC: "onequarter", 0xBD: "onehalf",
    0xBE: "threequarters", 0xBF: "questiondown",
    0xC0: "Agrave", 0xC1: "Aacute", 0xC2: "Acircumflex",
    0xC3: "Atilde", 0xC4: "Adieresis", 0xC5: "Aring",
    0xC6: "AE", 0xC7: "Ccedilla", 0xC8: "Egrave",
    0xC9: "Eacute", 0xCA: "Ecircumflex", 0xCB: "Edieresis",
    0xCC: "Igrave", 0xCD: "Iacute", 0xCE: "Icircumflex",
    0xCF: "Idieresis", 0xD0: "Eth", 0xD1: "Ntilde",
    0xD2: "Ograve", 0xD3: "Oacute", 0xD4: "Ocircumflex",
    0xD5: "Otilde", 0xD6: "Odieresis", 0xD7: "multiply",
    0xD8: "Oslash", 0xD9: "Ugrave", 0xDA: "Uacute",
    0xDB: "Ucircumflex", 0xDC: "Udieresis", 0xDD: "Yacute",
    0xDE: "Thorn", 0xDF: "germandbls", 0xE0: "agrave",
    0xE1: "aacute", 0xE2: "acircumflex", 0xE3: "atilde",
    0xE4: "adieresis", 0xE5: "aring", 0xE6: "ae",
    0xE7: "ccedilla", 0xE8: "egrave", 0xE9: "eacute",
    0xEA: "ecircumflex", 0xEB: "edieresis", 0xEC: "igrave",
    0xED: "iacute", 0xEE: "icircumflex", 0xEF: "idieresis",
    0xF0: "eth", 0xF1: "ntilde", 0xF2: "ograve",
    0xF3: "oacute", 0xF4: "ocircumflex", 0xF5: "otilde",
    0xF6: "odieresis", 0xF7: "divide", 0xF8: "oslash",
    0xF9: "ugrave", 0xFA: "uacute", 0xFB: "ucircumflex",
    0xFC: "udieresis", 0xFD: "yacute", 0xFE: "thorn",
    0xFF: "ydieresis",
}


def base_encoding(name: str | None) -> dict[int, str]:
    if name is None:
        return {}
    if name == "WinAnsiEncoding":
        result = _ascii_names()
        result.update(_WINANSI_HIGH)
        return result
    if name == "StandardEncoding":
        return _standard_names()
    if name == "MacRomanEncoding":
        # ASCII positions are safe; non-ASCII MacRoman stays fail-closed until
        # the complete owned table is introduced.
        return _ascii_names()
    raise FontEncodingError(f"unsupported simple-font base encoding /{name}")


def parse_encoding(
    doc: PDFDocument,
    value: PDFObject | None,
    *,
    require_explicit: bool = True,
) -> dict[int, str]:
    if value is None:
        if require_explicit:
            raise FontEncodingError("simple font requires an explicit PDF Encoding")
        return {}
    value = resolve(doc, value)
    if isinstance(value, PDFName):
        return base_encoding(value.value)
    if not isinstance(value, PDFDict):
        raise FontEncodingError("font Encoding is neither a name nor dictionary")

    base_value = resolve(doc, value.get("BaseEncoding")) if value.get("BaseEncoding") is not None else None
    if base_value is None:
        base_name = None
    elif isinstance(base_value, PDFName):
        base_name = base_value.value
    else:
        raise FontEncodingError("Encoding /BaseEncoding is not a name")
    mapping = base_encoding(base_name)

    differences = resolve(doc, value.get("Differences")) if value.get("Differences") is not None else None
    if differences is None:
        if base_name is None and require_explicit:
            raise FontEncodingError(
                "Encoding dictionary without BaseEncoding requires Differences for owned mapping"
            )
        return mapping
    if not isinstance(differences, list):
        raise FontEncodingError("Encoding /Differences is not an array")

    current: int | None = None
    for item in differences:
        item = resolve(doc, item)
        if isinstance(item, bool):
            raise FontEncodingError("boolean is not valid in Encoding /Differences")
        if isinstance(item, (int, Decimal)):
            integer = int(item)
            if integer != item or not 0 <= integer <= 255:
                raise FontEncodingError("Differences code shall be integer 0..255")
            current = integer
            continue
        if not isinstance(item, PDFName) or current is None:
            raise FontEncodingError("malformed Encoding /Differences array")
        if current > 255:
            raise FontEncodingError("Encoding /Differences runs past character code 255")
        mapping[current] = item.value
        current += 1
    return mapping
