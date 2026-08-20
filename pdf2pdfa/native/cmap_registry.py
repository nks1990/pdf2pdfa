"""Owned predefined-CMap registry and Type0 encoding resolver.

Identity-H/V are algorithmic. Other names are available only when their
compiled mapping data is versioned in :mod:`predefined_cmap_data`. No system
CMap lookup, network access or external font/PDF library is ever used.

Embedded CMap streams may inherit a base through either their stream dictionary
`/UseCMap` entry or a content-stream `/Name usecmap` operator. Recursion and
cycles are bounded explicitly.
"""

from __future__ import annotations

from functools import lru_cache

from .cmap import CIDCMap, CIDRange, CMapError, CodeSpace, NotDefRange
from .document import PDFDocument
from .objects import PDFName, PDFObject, PDFStream
from .predefined_cmap_data import CMAP_DATA
from .structure import decoded_stream_bytes, resolve


_MAX_CMAP_DEPTH = 32


def _compiled_cmap(name: str, stack: tuple[str, ...]) -> CIDCMap:
    if name in stack:
        chain = " -> ".join((*stack, name))
        raise CMapError(f"compiled predefined CMap inheritance cycle: {chain}")
    raw = CMAP_DATA.get(name)
    if raw is None:
        raise CMapError(
            f"predefined CMap /{name} is not present in the owned CMap registry"
        )
    if len(stack) >= _MAX_CMAP_DEPTH:
        raise CMapError(f"compiled CMap inheritance exceeds {_MAX_CMAP_DEPTH} levels")

    base_name = raw.get("base")
    base = None
    if base_name is not None:
        if not isinstance(base_name, str):
            raise CMapError(f"compiled CMap /{name} has invalid base metadata")
        base = _compiled_cmap(base_name, (*stack, name))

    def spaces():
        for item in raw.get("codespaces", ()):
            if not isinstance(item, tuple) or len(item) != 3:
                raise CMapError(f"compiled CMap /{name} has malformed codespace data")
            yield CodeSpace(int(item[0]), int(item[1]), int(item[2]))

    def point_map(key: str) -> dict[bytes, int]:
        result: dict[bytes, int] = {}
        for item in raw.get(key, ()):
            if not isinstance(item, tuple) or len(item) != 3:
                raise CMapError(f"compiled CMap /{name} has malformed {key} data")
            code, length, cid = int(item[0]), int(item[1]), int(item[2])
            if length <= 0 or code < 0 or code >= (1 << (8 * length)) or cid < 0:
                raise CMapError(f"compiled CMap /{name} has out-of-range {key} entry")
            result[code.to_bytes(length, "big")] = cid
        return result

    def cid_ranges():
        for item in raw.get("cid_ranges", ()):
            if not isinstance(item, tuple) or len(item) != 4:
                raise CMapError(f"compiled CMap /{name} has malformed cid_ranges data")
            yield CIDRange(int(item[0]), int(item[1]), int(item[2]), int(item[3]))

    def notdef_ranges():
        for item in raw.get("notdef_ranges", ()):
            if not isinstance(item, tuple) or len(item) != 4:
                raise CMapError(f"compiled CMap /{name} has malformed notdef_ranges data")
            yield NotDefRange(int(item[0]), int(item[1]), int(item[2]), int(item[3]))

    vertical = raw.get("vertical")
    if not isinstance(vertical, bool):
        raise CMapError(f"compiled CMap /{name} has invalid vertical metadata")

    return CIDCMap(
        codespaces=spaces(),
        cid_chars=point_map("cid_chars"),
        cid_ranges=cid_ranges(),
        notdef_chars=point_map("notdef_chars"),
        notdef_ranges=notdef_ranges(),
        vertical=vertical,
        base=base,
        name=name,
    )


@lru_cache(maxsize=256)
def predefined_cmap(name: str) -> CIDCMap:
    if name == "Identity-H":
        return CIDCMap.identity(vertical=False)
    if name == "Identity-V":
        return CIDCMap.identity(vertical=True)
    return _compiled_cmap(name, ())


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
