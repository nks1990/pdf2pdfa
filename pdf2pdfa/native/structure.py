"""High-level traversal helpers on top of the owned COS parser."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Iterator, Mapping

from .document import PDFDocument, PDFParseError
from .filters import decode_pipeline
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream, as_int


class PDFStructureError(PDFParseError):
    pass


@dataclass(frozen=True, slots=True)
class PageView:
    ref: PDFRef
    dictionary: PDFDict
    resources: PDFDict
    media_box: tuple[Decimal, Decimal, Decimal, Decimal]
    crop_box: tuple[Decimal, Decimal, Decimal, Decimal]
    rotate: int


def resolve(doc: PDFDocument, value: PDFObject | None) -> PDFObject | None:
    seen: set[PDFRef] = set()
    while isinstance(value, PDFRef):
        if value in seen:
            raise PDFStructureError(f"indirect reference cycle at {value}")
        seen.add(value)
        value = doc.get(value)
    return value


def resolve_dict(doc: PDFDocument, value: PDFObject | None, *, label: str) -> PDFDict:
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise PDFStructureError(f"{label} is not a dictionary")
    return value


def _number(value: PDFObject, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise PDFStructureError(f"{label} is not numeric")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    raise PDFStructureError(f"{label} is not numeric")


def _box(doc: PDFDocument, value: PDFObject | None, *, label: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != 4:
        raise PDFStructureError(f"{label} shall be an array of four numbers")
    return tuple(_number(item, label=label) for item in value)  # type: ignore[return-value]


def walk_pages(doc: PDFDocument) -> Iterator[PageView]:
    catalog = doc.catalog
    pages_ref = catalog.get("Pages")
    if not isinstance(pages_ref, PDFRef):
        raise PDFStructureError("catalog /Pages shall be an indirect reference")

    seen: set[PDFRef] = set()

    def visit(
        ref: PDFRef,
        inherited: dict[str, PDFObject],
    ) -> Iterator[PageView]:
        if ref in seen:
            raise PDFStructureError(f"page-tree cycle at {ref}")
        seen.add(ref)
        node = doc.get(ref)
        if not isinstance(node, PDFDict):
            raise PDFStructureError(f"page-tree object {ref} is not a dictionary")
        node_type = node.get("Type")
        name = node_type.value if isinstance(node_type, PDFName) else ""

        current = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                current[key] = node[key]

        if name == "Pages":
            kids = resolve(doc, node.get("Kids"))
            if not isinstance(kids, list):
                raise PDFStructureError(f"/Pages {ref} has invalid /Kids")
            for kid in kids:
                if not isinstance(kid, PDFRef):
                    raise PDFStructureError(f"/Pages {ref} contains a direct/non-reference kid")
                yield from visit(kid, current)
            return

        if name != "Page":
            raise PDFStructureError(f"page-tree node {ref} has invalid /Type {node_type}")
        resources_obj = resolve(doc, current.get("Resources", PDFDict()))
        if resources_obj is None:
            resources_obj = PDFDict()
        if not isinstance(resources_obj, PDFDict):
            raise PDFStructureError(f"page {ref} /Resources is not a dictionary")
        media = _box(doc, current.get("MediaBox"), label=f"page {ref} /MediaBox")
        crop = _box(
            doc,
            current.get("CropBox", current.get("MediaBox")),
            label=f"page {ref} /CropBox",
        )
        rotate_obj = resolve(doc, current.get("Rotate", 0))
        rotate = as_int(rotate_obj, 0)
        if rotate % 90:
            raise PDFStructureError(f"page {ref} /Rotate is not a multiple of 90")
        yield PageView(
            ref=ref,
            dictionary=node,
            resources=resources_obj,
            media_box=media,
            crop_box=crop,
            rotate=rotate % 360,
        )

    yield from visit(pages_ref, {})


def _filter_names(value: PDFObject | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, PDFName):
        return [value.value]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if not isinstance(item, PDFName):
                raise PDFStructureError("stream /Filter array contains non-name")
            names.append(item.value)
        return names
    raise PDFStructureError("stream /Filter is not a name or array")


def _decode_parms(value: PDFObject | None, count: int) -> list[Mapping[str, object] | None]:
    if value is None:
        return [None] * count
    if isinstance(value, PDFDict):
        return [value] + [None] * max(0, count - 1)
    if isinstance(value, list):
        result: list[Mapping[str, object] | None] = []
        for item in value:
            result.append(item if isinstance(item, PDFDict) else None)
        result.extend([None] * max(0, count - len(result)))
        return result[:count]
    return [None] * count


def decoded_stream_bytes(doc: PDFDocument, value: PDFObject, *, label: str = "stream") -> bytes:
    value = resolve(doc, value)
    if not isinstance(value, PDFStream):
        raise PDFStructureError(f"{label} is not a stream")
    filters = _filter_names(value.get("Filter"))
    parms = _decode_parms(value.get("DecodeParms"), len(filters))
    return decode_pipeline(value.data, filters, parms) if filters else value.data


def page_content_bytes(doc: PDFDocument, page: PageView) -> bytes:
    contents = resolve(doc, page.dictionary.get("Contents"))
    if contents is None:
        return b""
    if isinstance(contents, PDFStream):
        return decoded_stream_bytes(doc, contents, label=f"page {page.ref} /Contents")
    if isinstance(contents, list):
        chunks: list[bytes] = []
        for index, item in enumerate(contents):
            chunks.append(
                decoded_stream_bytes(
                    doc,
                    item,
                    label=f"page {page.ref} /Contents[{index}]",
                )
            )
        return b"\n".join(chunks)
    raise PDFStructureError(f"page {page.ref} /Contents is not a stream or stream array")


def iter_name_tree(doc: PDFDocument, tree: PDFObject | None) -> Iterator[tuple[bytes, PDFObject]]:
    tree = resolve(doc, tree)
    if tree is None:
        return
    if not isinstance(tree, PDFDict):
        raise PDFStructureError("name tree node is not a dictionary")
    names = resolve(doc, tree.get("Names"))
    if names is not None:
        if not isinstance(names, list) or len(names) % 2:
            raise PDFStructureError("name tree /Names is not an even-sized array")
        for index in range(0, len(names), 2):
            key = resolve(doc, names[index])
            if not isinstance(key, bytes):
                raise PDFStructureError("name tree key is not a string")
            yield key, names[index + 1]
    kids = resolve(doc, tree.get("Kids"))
    if kids is not None:
        if not isinstance(kids, list):
            raise PDFStructureError("name tree /Kids is not an array")
        for kid in kids:
            yield from iter_name_tree(doc, kid)


def walk_reachable_objects(doc: PDFDocument) -> Iterator[tuple[str, PDFObject]]:
    """Walk reachable direct values and indirect objects exactly once."""
    seen_refs: set[PDFRef] = set()
    seen_direct: set[int] = set()

    def visit(value: PDFObject, path: str) -> Iterator[tuple[str, PDFObject]]:
        if isinstance(value, PDFRef):
            if value in seen_refs:
                return
            seen_refs.add(value)
            target = doc.get(value)
            yield path + f" -> {value}", target
            yield from visit(target, path + f"/{value.object_number}")
            return
        if isinstance(value, PDFStream):
            identity = id(value)
            if identity in seen_direct:
                return
            seen_direct.add(identity)
            yield path, value
            yield from visit(value.dictionary, path + "/dict")
            return
        if isinstance(value, PDFDict):
            identity = id(value)
            if identity in seen_direct:
                return
            seen_direct.add(identity)
            yield path, value
            for key, child in value.items():
                yield from visit(child, path + "/" + key)
            return
        if isinstance(value, list):
            identity = id(value)
            if identity in seen_direct:
                return
            seen_direct.add(identity)
            yield path, value
            for index, child in enumerate(value):
                yield from visit(child, f"{path}[{index}]")
            return
        yield path, value

    yield from visit(doc.root_ref, "Root")
    info = doc.trailer.get("Info")
    if info is not None:
        yield from visit(info, "Info")
