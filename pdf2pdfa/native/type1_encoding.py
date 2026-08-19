"""Owned Type 1 built-in/PDF encoding semantics.

When a PDF Type1 font dictionary omits `/Encoding`, the font program's own
fontdict Encoding is authoritative.  If a PDF Encoding dictionary supplies
`/Differences` without `/BaseEncoding`, those differences are applied to the
font's built-in encoding, not to an empty map.

The Type 1 program parser recognizes deterministic StandardEncoding and
256-array/`dup code /name put` forms.  More general PostScript encoding
programs fail closed instead of invoking a PostScript VM.
"""

from __future__ import annotations

from decimal import Decimal
import re

from .document import PDFDocument
from .font_encoding import FontEncodingError, base_encoding
from .objects import PDFDict, PDFName, PDFObject
from .structure import resolve
from .type1 import Type1Error, _extract_eexec


class Type1EncodingError(Type1Error):
    pass


def parse_type1_builtin_encoding(data: bytes) -> dict[int, str]:
    clear, _ = _extract_eexec(bytes(data))
    match = re.search(
        rb"/Encoding\b(.*?)(?:readonly\s+)?def\b",
        clear,
        re.S,
    )
    if match is None:
        raise Type1EncodingError("Type1 font program has no parseable built-in /Encoding")

    body = match.group(1)
    if re.search(rb"\bStandardEncoding\b", body):
        try:
            mapping = base_encoding("StandardEncoding")
        except FontEncodingError as exc:
            raise Type1EncodingError(str(exc)) from exc
    elif re.search(rb"\b256\s+array\b", body):
        mapping = {}
    else:
        raise Type1EncodingError(
            "Type1 built-in Encoding requires unsupported PostScript evaluation"
        )

    for item in re.finditer(
        rb"\bdup\s+(\d{1,3})\s+/([^\s<>{}\[\]()/]+)\s+put\b",
        body,
    ):
        code = int(item.group(1))
        if not 0 <= code <= 255:
            raise Type1EncodingError("Type1 built-in Encoding code is outside 0..255")
        mapping[code] = item.group(2).decode("latin-1")

    return mapping


def parse_type1_pdf_encoding(
    doc: PDFDocument,
    value: PDFObject | None,
    *,
    built_in: dict[int, str],
) -> dict[int, str]:
    """Resolve a PDF Type1 Encoding using the font's built-in map as fallback."""
    if value is None:
        return dict(built_in)

    value = resolve(doc, value)
    if isinstance(value, PDFName):
        try:
            return base_encoding(value.value)
        except FontEncodingError as exc:
            raise Type1EncodingError(str(exc)) from exc
    if not isinstance(value, PDFDict):
        raise Type1EncodingError("Type1 PDF Encoding is neither a name nor dictionary")

    base_value = resolve(doc, value.get("BaseEncoding")) if value.get("BaseEncoding") is not None else None
    if base_value is None:
        mapping = dict(built_in)
    elif isinstance(base_value, PDFName):
        try:
            mapping = base_encoding(base_value.value)
        except FontEncodingError as exc:
            raise Type1EncodingError(str(exc)) from exc
    else:
        raise Type1EncodingError("Type1 Encoding /BaseEncoding is not a name")

    differences = resolve(doc, value.get("Differences")) if value.get("Differences") is not None else None
    if differences is None:
        return mapping
    if not isinstance(differences, list):
        raise Type1EncodingError("Type1 Encoding /Differences is not an array")

    current: int | None = None
    for item in differences:
        item = resolve(doc, item)
        if isinstance(item, bool):
            raise Type1EncodingError("boolean is not valid in Type1 Encoding /Differences")
        if isinstance(item, (int, Decimal)):
            integer = int(item)
            if integer != item or not 0 <= integer <= 255:
                raise Type1EncodingError("Type1 Differences code shall be integer 0..255")
            current = integer
            continue
        if not isinstance(item, PDFName) or current is None:
            raise Type1EncodingError("malformed Type1 Encoding /Differences array")
        if current > 255:
            raise Type1EncodingError("Type1 Encoding /Differences runs past code 255")
        mapping[current] = item.value
        current += 1
    return mapping
