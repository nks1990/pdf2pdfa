"""Owned predefined-CMap registry and Type0 encoding resolver.

Only CMaps whose mapping program is implemented in this repository may be
resolved by name.  Identity-H/V are algorithmic and therefore available now.
Other predefined Adobe CMaps remain fail-closed until their mapping tables are
added as owned source/data.

Embedded CMap streams may inherit a base through either their stream dictionary
`/UseCMap` entry or a content-stream `/Name usecmap` operator.  Recursion and
cycles are bounded explicitly.
"""

from __future__ import annotations

from .cmap import CIDCMap, CMapError
from .document import PDFDocument
from .objects import PDFName, PDFObject, PDFStream
from .structure import decoded_stream_bytes, resolve


_MAX_CMAP_DEPTH = 32


def predefined_cmap(name: str) -> CIDCMap:
    if name == "Identity-H":
        return CIDCMap.identity(vertical=False)
    if name == "Identity-V":
        return CIDCMap.identity(vertical=True)
    raise CMapError(
        f"predefined CMap /{name} is not present in the owned CMap registry"
    )


def _resolve_stream_cmap(
    doc: PDFDocument,
    stream: PDFStream,
    *,
    seen: set[int],
    depth: int,
) -> CIDCMap:
    if depth > _MAX_CMAP_DEPTH:
        raise CMapError(f"CMap inheritance exceeds {_MAX_CMAP_DEPTH} levels")
    identity = id(stream)
    if identity in seen:
        raise CMapError("CMap /UseCMap inheritance cycle detected")
    seen.add(identity)
    try:
        base: CIDCMap | None = None
        raw_use = resolve(doc, stream.get("UseCMap")) if stream.get("UseCMap") is not None else None
        if isinstance(raw_use, PDFName):
            base = predefined_cmap(raw_use.value)
        elif isinstance(raw_use, PDFStream):
            base = _resolve_stream_cmap(
                doc,
                raw_use,
                seen=seen,
                depth=depth + 1,
            )
        elif raw_use is not None:
            raise CMapError("CMap stream /UseCMap is neither a name nor a stream")

        data = decoded_stream_bytes(doc, stream, label="Type0 Encoding CMap")
        return CIDCMap.parse(
            data,
            registry=predefined_cmap,
            base=base,
        )
    finally:
        seen.remove(identity)


def resolve_type0_cmap(doc: PDFDocument, encoding_value: PDFObject | None) -> CIDCMap:
    """Resolve a Type0 `/Encoding` to one owned `CIDCMap` instance."""
    encoding = resolve(doc, encoding_value)
    if isinstance(encoding, PDFName):
        return predefined_cmap(encoding.value)
    if isinstance(encoding, PDFStream):
        return _resolve_stream_cmap(doc, encoding, seen=set(), depth=0)
    raise CMapError("Type0 font Encoding is missing or invalid")
