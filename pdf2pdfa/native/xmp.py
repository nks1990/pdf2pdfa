"""Pure-Python XMP reader/writer for PDF/A metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET


class XMPError(ValueError):
    pass


NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "pdf": "http://ns.adobe.com/pdf/1.3/",
    "pdfaid": "http://www.aiim.org/pdfa/ns/id/",
    "pdfaExtension": "http://www.aiim.org/pdfa/ns/extension/",
}
XML_NS = "http://www.w3.org/XML/1998/namespace"

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def _q(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def _description(root: ET.Element) -> ET.Element:
    rdf = root.find(".//" + _q("rdf", "RDF"))
    if rdf is None:
        rdf = ET.SubElement(root, _q("rdf", "RDF"))
    description = rdf.find(_q("rdf", "Description"))
    if description is None:
        description = ET.SubElement(rdf, _q("rdf", "Description"))
        description.set(_q("rdf", "about"), "")
    return description


def _strip_packet(data: bytes) -> bytes:
    start = data.find(b"<x:xmpmeta")
    if start < 0:
        start = data.find(b"<xmpmeta")
    if start < 0:
        start = data.find(b"<rdf:RDF")
    if start < 0:
        return data.strip()
    end_marker = b"</x:xmpmeta>"
    end = data.rfind(end_marker)
    if end >= 0:
        return data[start : end + len(end_marker)]
    return data[start:].strip()


@dataclass(slots=True)
class XMPDocument:
    root: ET.Element

    @classmethod
    def new(cls) -> "XMPDocument":
        root = ET.Element(_q("x", "xmpmeta"))
        _description(root)
        return cls(root)

    @classmethod
    def parse(cls, data: bytes) -> "XMPDocument":
        try:
            root = ET.fromstring(_strip_packet(data))
        except ET.ParseError as exc:
            raise XMPError(f"invalid XMP XML: {exc}") from exc
        if root.tag == _q("rdf", "RDF"):
            wrapper = ET.Element(_q("x", "xmpmeta"))
            wrapper.append(root)
            root = wrapper
        return cls(root)

    @property
    def description(self) -> ET.Element:
        return _description(self.root)

    def get(self, prefix: str, local: str) -> str | None:
        tag = _q(prefix, local)
        desc = self.description
        if tag in desc.attrib:
            return desc.attrib[tag]
        element = desc.find(tag)
        if element is None:
            return None
        if element.text and element.text.strip():
            return element.text.strip()
        # Dublin Core structured values.
        for container_name in ("Alt", "Seq", "Bag"):
            container = element.find(_q("rdf", container_name))
            if container is not None:
                values = [
                    (li.text or "").strip()
                    for li in container.findall(_q("rdf", "li"))
                    if (li.text or "").strip()
                ]
                if values:
                    return "; ".join(values)
        return None

    def set_simple(self, prefix: str, local: str, value: str) -> None:
        desc = self.description
        tag = _q(prefix, local)
        desc.attrib.pop(tag, None)
        element = desc.find(tag)
        if element is None:
            element = ET.SubElement(desc, tag)
        else:
            element.clear()
        element.text = value

    def set_alt(self, prefix: str, local: str, value: str) -> None:
        desc = self.description
        tag = _q(prefix, local)
        desc.attrib.pop(tag, None)
        element = desc.find(tag)
        if element is None:
            element = ET.SubElement(desc, tag)
        else:
            element.clear()
        alt = ET.SubElement(element, _q("rdf", "Alt"))
        li = ET.SubElement(alt, _q("rdf", "li"))
        li.set(f"{{{XML_NS}}}lang", "x-default")
        li.text = value

    def set_seq(self, prefix: str, local: str, values: list[str]) -> None:
        desc = self.description
        tag = _q(prefix, local)
        desc.attrib.pop(tag, None)
        element = desc.find(tag)
        if element is None:
            element = ET.SubElement(desc, tag)
        else:
            element.clear()
        seq = ET.SubElement(element, _q("rdf", "Seq"))
        for value in values:
            li = ET.SubElement(seq, _q("rdf", "li"))
            li.text = value

    def namespaces(self) -> set[str]:
        result: set[str] = set()
        for element in self.root.iter():
            if element.tag.startswith("{"):
                result.add(element.tag[1:].split("}", 1)[0])
            for attr in element.attrib:
                if attr.startswith("{"):
                    result.add(attr[1:].split("}", 1)[0])
        return result

    def has_extension_schema(self) -> bool:
        uri = NS["pdfaExtension"]
        return any(
            element.tag.startswith("{" + uri + "}")
            or any(attr.startswith("{" + uri + "}") for attr in element.attrib)
            for element in self.root.iter()
        )

    def to_bytes(self) -> bytes:
        xml = ET.tostring(self.root, encoding="utf-8", xml_declaration=False)
        return (
            b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            + xml
            + b'\n<?xpacket end="w"?>'
        )


def pdf_date_to_iso(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            text = value.decode("latin-1")
        except UnicodeDecodeError:
            return None
    else:
        text = value
    text = text.strip()
    if text.startswith("D:"):
        text = text[2:]
    if len(text) < 4 or not text[:4].isdigit():
        return None
    digits = "".join(ch for ch in text[:14] if ch.isdigit())
    fields = [int(digits[0:4])]
    defaults = [1, 1, 0, 0, 0]
    position = 4
    for default in defaults:
        if position + 2 <= len(digits):
            fields.append(int(digits[position : position + 2]))
            position += 2
        else:
            fields.append(default)
    try:
        dt = datetime(*fields, tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def build_pdfa_xmp(
    *,
    part: int,
    conformance: str,
    info: dict[str, bytes] | None = None,
    producer: str = "pdf2pdfa native engine",
    existing: bytes | None = None,
    now: datetime | None = None,
) -> bytes:
    if part not in (1, 2, 3):
        raise ValueError("PDF/A part must be 1, 2 or 3")
    conformance = conformance.upper()
    if conformance != "B":
        raise ValueError("native engine currently emits Level B identification")
    if existing:
        try:
            document = XMPDocument.parse(existing)
        except XMPError:
            document = XMPDocument.new()
    else:
        document = XMPDocument.new()

    document.set_simple("pdfaid", "part", str(part))
    document.set_simple("pdfaid", "conformance", conformance)
    document.set_simple("dc", "format", "application/pdf")
    document.set_simple("pdf", "Producer", producer)
    timestamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    document.set_simple("xmp", "ModifyDate", timestamp)
    document.set_simple("xmp", "MetadataDate", timestamp)

    info = info or {}
    if info.get("Title"):
        document.set_alt("dc", "title", info["Title"].decode("latin-1", "replace"))
    if info.get("Author"):
        document.set_seq("dc", "creator", [info["Author"].decode("latin-1", "replace")])
    if info.get("Subject"):
        document.set_alt("dc", "description", info["Subject"].decode("latin-1", "replace"))
    if info.get("Keywords"):
        document.set_simple("pdf", "Keywords", info["Keywords"].decode("latin-1", "replace"))
    if info.get("Creator"):
        document.set_simple("xmp", "CreatorTool", info["Creator"].decode("latin-1", "replace"))
    creation = pdf_date_to_iso(info.get("CreationDate"))
    if creation:
        document.set_simple("xmp", "CreateDate", creation)
    return document.to_bytes()
