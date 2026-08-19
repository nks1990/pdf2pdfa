"""Owned parser for the built-in Encoding of embedded Type 1 programs.

When a PDF Type1 font dictionary omits `/Encoding`, the font program's own
fontdict Encoding is authoritative.  Type 1 programs commonly use Adobe
StandardEncoding directly or construct a 256-entry array with `dup code /name
put` statements.  This module recognizes those deterministic forms and fails
closed on PostScript programs that would require a general PostScript VM.
"""

from __future__ import annotations

import re

from .font_encoding import FontEncodingError, base_encoding
from .type1 import Type1Error, _extract_eexec


class Type1EncodingError(Type1Error):
    pass


_NAME = rb"[^\s<>{}\[\]()/]+"


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

    for item in re.finditer(rb"\bdup\s+(\d{1,3})\s+/([^\s<>{}\[\]()/]+)\s+put\b", body):
        code = int(item.group(1))
        if not 0 <= code <= 255:
            raise Type1EncodingError("Type1 built-in Encoding code is outside 0..255")
        mapping[code] = item.group(2).decode("latin-1")

    return mapping
