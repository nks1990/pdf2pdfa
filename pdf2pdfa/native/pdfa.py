"""Owned PDF/A-1b, PDF/A-2b and PDF/A-3b conformance engine.

No external validator or PDF library participates in the decision.  The module
runs on the pure-Python COS/content/ICC/XMP stack in ``pdf2pdfa.native``.

The validator is intentionally fail-closed: malformed or unsupported syntax
that matters for a conformance decision produces a rule failure rather than an
optimistic pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

from .content import ContentInstruction, ContentStreamError, InlineImage, parse_content_stream
from .document import PDFDocument, PDFParseError
from .icc import ICCError, ICCProfile, parse_icc
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream, as_int
from .structure import (
    PDFStructureError,
    PageView,
    decoded_stream_bytes,
    iter_name_tree,
    page_content_bytes,
    resolve,
    resolve_dict,
    walk_pages,
    walk_reachable_objects,
)
from .xmp import NS, XMPDocument, XMPError


SUPPORTED_LEVELS = {"1b", "2b", "3b"}


@dataclass(frozen=True, slots=True)
class PDFAPolicy:
    level: str

    @property
    def part(self) -> int:
        return int(self.level[0])

    @property
    def conformance(self) -> str:
        return self.level[1].upper()

    @property
    def allow_transparency(self) -> bool:
        return self.part >= 2

    @property
    def attachment_mode(self) -> str:
        if self.part == 1:
            return "none"
        if self.part == 2:
            return "pdfa-or-text"
        return "arbitrary"


def policy(level: str) -> PDFAPolicy:
    normalized = level.lower()
    if normalized not in SUPPORTED_LEVELS:
        raise ValueError(
            f"unsupported PDF/A level {level!r}; expected one of {sorted(SUPPORTED_LEVELS)}"
        )
    return PDFAPolicy(normalized)


@dataclass(frozen=True, slots=True)
class RuleFailure:
    rule_id: str
    clause: str
    message: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    level: str
    compliant: bool
    passed_checks: int
    failures: tuple[RuleFailure, ...]
    engine: str = "pdf2pdfa-owned"

    @property
    def failed_checks(self) -> int:
        return len(self.failures)

    @property
    def failed_rules(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(failure.rule_id for failure in self.failures))


class _Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[RuleFailure] = []
        self._dedupe: set[tuple[str, str, str]] = set()

    def check(
        self,
        condition: bool,
        rule_id: str,
        clause: str,
        message: str,
        path: str = "",
    ) -> bool:
        if condition:
            self.passed += 1
            return True
        key = (rule_id, path, message)
        if key not in self._dedupe:
            self._dedupe.add(key)
            self.failures.append(RuleFailure(rule_id, clause, message, path))
        return False

    def fail(self, rule_id: str, clause: str, message: str, path: str = "") -> None:
        self.check(False, rule_id, clause, message, path)


@dataclass(frozen=True, slots=True)
class _OutputIntent:
    profile: ICCProfile
    path: str


@dataclass(slots=True)
class _Usage:
    fonts: list[tuple[PDFObject, str]] = field(default_factory=list)
    device_spaces: list[tuple[str, str, PDFDict]] = field(default_factory=list)
    icc_spaces: list[tuple[PDFObject, str]] = field(default_factory=list)
    transparency: list[str] = field(default_factory=list)
    filespecs: list[tuple[PDFObject, str]] = field(default_factory=list)
    _content_seen: set[str] = field(default_factory=set)


_FORBIDDEN_ACTIONS = {
    "JavaScript",
    "Launch",
    "Sound",
    "Movie",
    "ResetForm",
    "ImportData",
    "Hide",
    "Rendition",
    "Trans",
    "GoTo3DView",
    "SetState",
    "NoOp",
}

_DYNAMIC_ANNOTATIONS = {"Sound", "Movie", "Screen", "3D", "RichMedia"}
_ALLOWED_AF_RELATIONSHIPS = {"Source", "Data", "Alternative", "Supplement", "Unspecified"}
_STANDARD_XMP_NAMESPACES = {
    NS["x"],
    NS["rdf"],
    NS["dc"],
    NS["xmp"],
    NS["pdf"],
    NS["pdfaid"],
    NS["pdfaExtension"],
    "http://ns.adobe.com/xap/1.0/mm/",
    "http://ns.adobe.com/xap/1.0/rights/",
    "http://ns.adobe.com/photoshop/1.0/",
    "http://www.aiim.org/pdfa/ns/schema#",
    "http://www.aiim.org/pdfa/ns/property#",
    "http://www.aiim.org/pdfa/ns/type#",
    "http://www.aiim.org/pdfa/ns/field#",
}


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _stream(doc: PDFDocument, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _array(doc: PDFDocument, value: PDFObject | None) -> list[PDFObject] | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, list) else None


def _filter_names(doc: PDFDocument, stream: PDFStream) -> list[str]:
    try:
        value = resolve(doc, stream.get("Filter"))
    except Exception:
        return ["<unresolvable>"]
    if value is None:
        return []
    if isinstance(value, PDFName):
        return [value.value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            try:
                item = resolve(doc, item)
            except Exception:
                result.append("<unresolvable>")
                continue
            result.append(item.value if isinstance(item, PDFName) else "<non-name>")
        return result
    return ["<invalid-filter>"]


def _info_bytes(doc: PDFDocument) -> dict[str, bytes]:
    info = _dict(doc, doc.trailer.get("Info"))
    if not info:
        return {}
    output: dict[str, bytes] = {}
    for key in (
        "Title",
        "Author",
        "Subject",
        "Keywords",
        "Creator",
        "Producer",
        "CreationDate",
        "ModDate",
    ):
        try:
            value = resolve(doc, info.get(key))
        except Exception:
            continue
        if isinstance(value, bytes):
            output[key] = value
    return output


def _text(value: bytes) -> str:
    if value.startswith(b"\xfe\xff") and len(value) % 2 == 0:
        try:
            return value[2:].decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return value.decode("latin-1", "replace")


def _same_metadata(info_value: bytes, xmp_value: str | None) -> bool:
    if xmp_value is None:
        return False
    text = _text(info_value).strip()
    xmp = xmp_value.strip()
    return text == xmp or text in [part.strip() for part in xmp.split(";")]


def _is_number(value: PDFObject | None) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _number(value: PDFObject | None, default: float) -> float:
    if _is_number(value):
        return float(value)  # type: ignore[arg-type]
    return default


def _resource_dict(doc: PDFDocument, resources: PDFObject | None) -> PDFDict:
    try:
        resolved = resolve(doc, resources)
    except Exception:
        return PDFDict()
    return resolved if isinstance(resolved, PDFDict) else PDFDict()


def _resource(doc: PDFDocument, resources: PDFDict, category: str, name: PDFObject) -> PDFObject | None:
    if not isinstance(name, PDFName):
        return None
    category_dict = _dict(doc, resources.get(category))
    if category_dict is None:
        return None
    return category_dict.get(name.value)


def _default_space(doc: PDFDocument, resources: PDFDict, device: str) -> PDFObject | None:
    colors = _dict(doc, resources.get("ColorSpace"))
    if colors is None:
        return None
    return colors.get("Default" + device)


def _color_space_family(doc: PDFDocument, value: PDFObject | None) -> str:
    try:
        value = resolve(doc, value)
    except Exception:
        return "<unresolvable>"
    if isinstance(value, PDFName):
        return value.value
    if isinstance(value, list) and value:
        try:
            first = resolve(doc, value[0])
        except Exception:
            return "<unresolvable>"
        return first.value if isinstance(first, PDFName) else "<invalid>"
    return "<invalid>"


class _NativeSession:
    def __init__(self, doc: PDFDocument, level: str, *, depth: int = 0) -> None:
        self.doc = doc
        self.policy = policy(level)
        self.checks = _Checks()
        self.usage = _Usage()
        self.depth = depth
        self.output_intents: list[_OutputIntent] = []

    def run(self) -> ValidationReport:
        self._file_rules()
        self._metadata_rules()
        self._reachable_object_rules()
        self._page_rules()
        self._font_rules()
        self._color_rules()
        self._attachment_rules()
        self._form_rules()
        failures = tuple(self.checks.failures)
        return ValidationReport(
            level=self.policy.level,
            compliant=not failures,
            passed_checks=self.checks.passed,
            failures=failures,
        )

    def _file_rules(self) -> None:
        self.checks.check(
            "Encrypt" not in self.doc.trailer,
            "file.encryption",
            "ISO 19005 encryption",
            "PDF/A files shall not be encrypted.",
            "trailer/Encrypt",
        )

        try:
            major, minor = (int(part) for part in self.doc.header_version.split(".", 1))
        except Exception:
            major, minor = (99, 99)
        if self.policy.part == 1:
            self.checks.check(
                (major, minor) <= (1, 4),
                "pdfa1.pdf_version",
                "ISO 19005-1 file format",
                f"PDF/A-1 is based on PDF 1.4; found PDF {self.doc.header_version}.",
                "header",
            )
            try:
                startxref = self.doc._find_startxref()
                pos = startxref
                while pos < len(self.doc.data) and self.doc.data[pos] in b"\x00\x09\x0a\x0c\x0d\x20":
                    pos += 1
                classic = self.doc.data.startswith(b"xref", pos)
            except Exception:
                classic = False
            self.checks.check(
                classic,
                "pdfa1.xref_stream",
                "ISO 19005-1 syntax restrictions",
                "PDF/A-1 shall use a classic cross-reference table, not an xref stream.",
                "startxref",
            )
            self.checks.check(
                not any(entry.kind == 2 for entry in self.doc.xref.values()),
                "pdfa1.object_stream",
                "ISO 19005-1 syntax restrictions",
                "PDF/A-1 shall not use compressed object streams.",
                "xref",
            )
        else:
            self.checks.check(
                (major, minor) <= (1, 7),
                "pdfa23.pdf_version",
                "ISO 19005-2/3 file format",
                f"PDF/A-{self.policy.part} is based on PDF 1.7; found PDF {self.doc.header_version}.",
                "header",
            )

        for path, value in walk_reachable_objects(self.doc):
            if not isinstance(value, PDFStream):
                continue
            filters = _filter_names(self.doc, value)
            self.checks.check(
                not any(name in ("LZWDecode", "LZW") for name in filters),
                "stream.lzw",
                "ISO 19005 stream filters",
                "LZW compression is prohibited in PDF/A.",
                path,
            )
            self.checks.check(
                "Crypt" not in filters,
                "stream.crypt_filter",
                "ISO 19005 encryption",
                "Crypt stream filters are prohibited in PDF/A.",
                path,
            )
            self.checks.check(
                not any(key in value.dictionary for key in ("F", "FFilter", "FDecodeParms")),
                "stream.external_data",
                "ISO 19005 external references",
                "Stream bytes shall be embedded in the PDF; external stream data is prohibited.",
                path,
            )
            if self.policy.part == 1:
                self.checks.check(
                    "JPXDecode" not in filters,
                    "pdfa1.jpx",
                    "ISO 19005-1 permitted PDF 1.4 feature set",
                    "JPEG 2000 / JPXDecode is not available to PDF/A-1.",
                    path,
                )

    def _metadata_rules(self) -> None:
        catalog = self.doc.catalog
        metadata_value = catalog.get("Metadata")
        metadata = _stream(self.doc, metadata_value)
        if not self.checks.check(
            metadata is not None,
            "metadata.required",
            "ISO 19005 metadata",
            "Document-level XMP metadata is required.",
            "catalog/Metadata",
        ):
            return
        assert metadata is not None
        self.checks.check(
            _name(metadata.get("Type")) in ("", "Metadata"),
            "metadata.type",
            "ISO 19005 metadata",
            "Metadata stream /Type shall be /Metadata when present.",
            "catalog/Metadata",
        )
        self.checks.check(
            _name(metadata.get("Subtype")) == "XML",
            "metadata.subtype",
            "ISO 19005 metadata",
            "Metadata stream /Subtype shall be /XML.",
            "catalog/Metadata",
        )
        try:
            raw = decoded_stream_bytes(self.doc, metadata, label="catalog/Metadata")
            xmp = XMPDocument.parse(raw)
        except (PDFStructureError, XMPError, ValueError) as exc:
            self.checks.fail(
                "metadata.xml",
                "ISO 19005 metadata",
                f"XMP metadata cannot be parsed: {exc}",
                "catalog/Metadata",
            )
            return

        self.checks.check(
            xmp.get("pdfaid", "part") == str(self.policy.part),
            "metadata.pdfaid_part",
            "ISO 19005 identification",
            f"pdfaid:part shall be {self.policy.part}.",
            "catalog/Metadata",
        )
        self.checks.check(
            (xmp.get("pdfaid", "conformance") or "").upper() == self.policy.conformance,
            "metadata.pdfaid_conformance",
            "ISO 19005 identification",
            f"pdfaid:conformance shall be {self.policy.conformance}.",
            "catalog/Metadata",
        )
        self.checks.check(
            xmp.get("dc", "format") == "application/pdf",
            "metadata.dc_format",
            "ISO 19005 metadata",
            "dc:format shall be application/pdf.",
            "catalog/Metadata",
        )
        custom = {
            uri
            for uri in xmp.namespaces()
            if uri not in _STANDARD_XMP_NAMESPACES
            and uri != "http://www.w3.org/XML/1998/namespace"
        }
        self.checks.check(
            not custom or xmp.has_extension_schema(),
            "metadata.extension_schema",
            "ISO 19005 XMP extension schemas",
            "Custom XMP namespaces require a PDF/A extension-schema declaration: "
            + ", ".join(sorted(custom)),
            "catalog/Metadata",
        )

        info = _info_bytes(self.doc)
        sync = {
            "Title": ("dc", "title"),
            "Author": ("dc", "creator"),
            "Subject": ("dc", "description"),
            "Keywords": ("pdf", "Keywords"),
            "Creator": ("xmp", "CreatorTool"),
            "Producer": ("pdf", "Producer"),
        }
        for key, (prefix, local) in sync.items():
            if key not in info:
                continue
            self.checks.check(
                _same_metadata(info[key], xmp.get(prefix, local)),
                "metadata.info_sync",
                "ISO 19005 metadata consistency",
                f"DocumentInfo /{key} is not synchronized with XMP {prefix}:{local}.",
                f"Info/{key}",
            )

    def _reachable_object_rules(self) -> None:
        catalog = self.doc.catalog
        if self.policy.part == 1:
            self.checks.check(
                catalog.get("OCProperties") is None,
                "pdfa1.optional_content",
                "ISO 19005-1 PDF 1.4 feature restrictions",
                "Optional-content groups/layers are not supported in PDF/A-1.",
                "catalog/OCProperties",
            )

        for path, value in walk_reachable_objects(self.doc):
            dictionary: PDFDict | None = None
            if isinstance(value, PDFStream):
                dictionary = value.dictionary
            elif isinstance(value, PDFDict):
                dictionary = value
            if dictionary is None:
                continue

            if _name(dictionary.get("Type")) == "Filespec" or dictionary.get("EF") is not None:
                self.usage.filespecs.append((dictionary, path))

            if _name(dictionary.get("Type")) == "XObject" and _name(dictionary.get("Subtype")) == "PS":
                self.checks.fail(
                    "graphics.postscript_xobject",
                    "ISO 19005 graphics",
                    "PostScript XObjects are prohibited in PDF/A.",
                    path,
                )
            if _name(dictionary.get("Subtype")) == "Image":
                self.checks.check(
                    dictionary.get("Alternates") is None,
                    "graphics.alternate_images",
                    "ISO 19005 graphics",
                    "Alternate images are prohibited in PDF/A.",
                    path,
                )
                if self.policy.part == 1 and (
                    dictionary.get("SMask") is not None
                    or isinstance(dictionary.get("Mask"), PDFRef)
                    or isinstance(dictionary.get("Mask"), PDFStream)
                ):
                    self.usage.transparency.append(path + "/image-mask")

            for key in ("OPI", "Ref"):
                if dictionary.get(key) is not None:
                    self.checks.fail(
                        "graphics.external_reference",
                        "ISO 19005 external rendering references",
                        f"/{key} creates an external rendering dependency.",
                        path + "/" + key,
                    )

            for key in ("TR", "TR2"):
                transfer = dictionary.get(key)
                if transfer is not None:
                    self.checks.check(
                        _name(transfer) == "Default",
                        "graphics.transfer_function",
                        "ISO 19005 graphics state",
                        f"/{key} shall be absent or /Default.",
                        path + "/" + key,
                    )

            self._action_rules(dictionary, path)

    def _action_rules(self, dictionary: PDFDict, path: str) -> None:
        action_type = _name(dictionary.get("S"))
        if action_type in _FORBIDDEN_ACTIONS:
            self.checks.fail(
                "actions.forbidden",
                "ISO 19005 interactive features",
                f"Action /{action_type} is prohibited in PDF/A-{self.policy.part}.",
                path,
            )
        aa = _dict(self.doc, dictionary.get("AA"))
        if aa:
            for event, candidate in aa.items():
                action = _dict(self.doc, candidate)
                if action:
                    action_type = _name(action.get("S"))
                    if action_type in _FORBIDDEN_ACTIONS:
                        self.checks.fail(
                            "actions.forbidden_additional_action",
                            "ISO 19005 interactive features",
                            f"Additional action /{event} uses prohibited /{action_type}.",
                            path + "/AA/" + event,
                        )

    def _page_rules(self) -> None:
        try:
            pages = list(walk_pages(self.doc))
        except PDFStructureError as exc:
            self.checks.fail(
                "pages.structure",
                "ISO 32000 page tree",
                f"Page tree cannot be interpreted: {exc}",
                "catalog/Pages",
            )
            return
        self.checks.check(
            bool(pages),
            "pages.present",
            "ISO 32000 page tree",
            "Document shall contain at least one page.",
            "catalog/Pages",
        )
        for index, page in enumerate(pages, start=1):
            path = f"page[{index}]"
            annots = _array(self.doc, page.dictionary.get("Annots")) or []
            for annot_index, annot_value in enumerate(annots, start=1):
                annot = _dict(self.doc, annot_value)
                if annot is None:
                    self.checks.fail(
                        "annotations.dictionary",
                        "ISO 32000 annotations",
                        "Annotation entry is not a dictionary.",
                        f"{path}/Annots[{annot_index}]",
                    )
                    continue
                self._annotation_rules(annot, f"{path}/Annots[{annot_index}]", page.resources)

            try:
                content = page_content_bytes(self.doc, page)
                self._scan_content(content, page.resources, path + "/Contents")
            except (PDFStructureError, ContentStreamError, ValueError) as exc:
                self.checks.fail(
                    "content.parse",
                    "ISO 32000 content streams",
                    f"Page content cannot be parsed: {exc}",
                    path + "/Contents",
                )

        if self.policy.part == 1:
            for location in self.usage.transparency:
                self.checks.fail(
                    "pdfa1.transparency",
                    "ISO 19005-1 transparency",
                    "Transparency is prohibited in PDF/A-1.",
                    location,
                )

    def _annotation_rules(self, annot: PDFDict, path: str, inherited_resources: PDFDict) -> None:
        subtype = _name(annot.get("Subtype"))
        self.checks.check(
            subtype not in _DYNAMIC_ANNOTATIONS,
            "annotations.dynamic",
            "ISO 19005 annotations",
            f"Dynamic annotation /{subtype} is prohibited.",
            path,
        )
        flags = as_int(resolve(self.doc, annot.get("F")) if annot.get("F") is not None else 0, 0)
        if subtype and subtype != "Popup":
            print_flag = bool(flags & 4)
            hidden = bool(flags & 2)
            invisible = bool(flags & 1)
            no_view = bool(flags & 32)
            self.checks.check(
                print_flag and not hidden and not invisible and not no_view,
                "annotations.visibility",
                "ISO 19005 annotations",
                "Annotations shall be printable and not hidden, invisible or NoView.",
                path,
            )
        if self.policy.part == 1 and subtype == "FileAttachment":
            self.checks.fail(
                "pdfa1.file_attachment_annotation",
                "ISO 19005-1 embedded files",
                "FileAttachment annotations are prohibited in PDF/A-1.",
                path,
            )
        self._action_rules(annot, path)

        appearances = _dict(self.doc, annot.get("AP"))
        if appearances:
            for key, appearance_value in appearances.items():
                appearance = _stream(self.doc, appearance_value)
                if appearance is None:
                    # /N may be a subdictionary keyed by appearance state.
                    states = _dict(self.doc, appearance_value)
                    if states:
                        for state, state_value in states.items():
                            stream = _stream(self.doc, state_value)
                            if stream:
                                resources = _resource_dict(
                                    self.doc,
                                    stream.get("Resources") or inherited_resources,
                                )
                                try:
                                    self._scan_content(
                                        decoded_stream_bytes(self.doc, stream),
                                        resources,
                                        f"{path}/AP/{key}/{state}",
                                    )
                                except Exception as exc:
                                    self.checks.fail(
                                        "annotations.appearance_parse",
                                        "ISO 32000 annotation appearances",
                                        f"Appearance stream cannot be parsed: {exc}",
                                        f"{path}/AP/{key}/{state}",
                                    )
                    continue
                resources = _resource_dict(
                    self.doc,
                    appearance.get("Resources") or inherited_resources,
                )
                try:
                    self._scan_content(
                        decoded_stream_bytes(self.doc, appearance),
                        resources,
                        f"{path}/AP/{key}",
                    )
                except Exception as exc:
                    self.checks.fail(
                        "annotations.appearance_parse",
                        "ISO 32000 annotation appearances",
                        f"Appearance stream cannot be parsed: {exc}",
                        f"{path}/AP/{key}",
                    )

    def _scan_content(self, data: bytes, resources: PDFDict, path: str) -> None:
        signature = f"{path}:{hash(data)}"
        if signature in self.usage._content_seen:
            return
        self.usage._content_seen.add(signature)
        items = parse_content_stream(data)
        for item_index, item in enumerate(items):
            item_path = f"{path}#{item_index}"
            if isinstance(item, InlineImage):
                color = item.dictionary.get("CS", item.dictionary.get("ColorSpace"))
                self._record_color_space(color, resources, item_path + "/inline-image")
                continue
            operator = item.operator
            operands = item.operands
            if operator == "Tf" and operands:
                font = _resource(self.doc, resources, "Font", operands[0])
                if font is None:
                    self.checks.fail(
                        "fonts.resource_missing",
                        "ISO 32000 text",
                        f"Font resource {operands[0]} is selected but not defined.",
                        item_path,
                    )
                else:
                    self.usage.fonts.append((font, item_path))
            elif operator in ("rg", "RG"):
                self._record_device("RGB", resources, item_path)
            elif operator in ("k", "K"):
                self._record_device("CMYK", resources, item_path)
            elif operator in ("g", "G"):
                self._record_device("Gray", resources, item_path)
            elif operator in ("cs", "CS") and operands:
                self._record_color_space(operands[-1], resources, item_path)
            elif operator == "Do" and operands:
                xobject_value = _resource(self.doc, resources, "XObject", operands[0])
                xobject = _stream(self.doc, xobject_value)
                if xobject is None:
                    self.checks.fail(
                        "graphics.xobject_missing",
                        "ISO 32000 graphics",
                        f"XObject resource {operands[0]} is missing or not a stream.",
                        item_path,
                    )
                    continue
                subtype = _name(xobject.get("Subtype"))
                if subtype == "Image":
                    self._record_color_space(xobject.get("ColorSpace"), resources, item_path + "/Image")
                    if self.policy.part == 1 and (
                        xobject.get("SMask") is not None or xobject.get("Mask") is not None
                    ):
                        self.usage.transparency.append(item_path + "/Image")
                elif subtype == "Form":
                    group = _dict(self.doc, xobject.get("Group"))
                    if self.policy.part == 1 and group and _name(group.get("S")) == "Transparency":
                        self.usage.transparency.append(item_path + "/FormGroup")
                    nested_resources = _resource_dict(
                        self.doc,
                        xobject.get("Resources") or resources,
                    )
                    try:
                        self._scan_content(
                            decoded_stream_bytes(self.doc, xobject),
                            nested_resources,
                            item_path + "/Form",
                        )
                    except Exception as exc:
                        self.checks.fail(
                            "graphics.form_parse",
                            "ISO 32000 form XObjects",
                            f"Form XObject cannot be parsed: {exc}",
                            item_path,
                        )
            elif operator == "gs" and operands:
                state_value = _resource(self.doc, resources, "ExtGState", operands[0])
                state = _dict(self.doc, state_value)
                if state is None:
                    self.checks.fail(
                        "graphics.extgstate_missing",
                        "ISO 32000 graphics state",
                        f"ExtGState resource {operands[0]} is missing or invalid.",
                        item_path,
                    )
                else:
                    self._graphics_state(state, item_path)

    def _graphics_state(self, state: PDFDict, path: str) -> None:
        ca = _number(resolve(self.doc, state.get("ca")) if state.get("ca") is not None else None, 1.0)
        CA = _number(resolve(self.doc, state.get("CA")) if state.get("CA") is not None else None, 1.0)
        smask = resolve(self.doc, state.get("SMask")) if state.get("SMask") is not None else None
        blend = resolve(self.doc, state.get("BM")) if state.get("BM") is not None else PDFName("Normal")
        blend_names: list[str] = []
        if isinstance(blend, PDFName):
            blend_names = [blend.value]
        elif isinstance(blend, list):
            blend_names = [_name(item) for item in blend]
        transparent = (
            ca < 1.0
            or CA < 1.0
            or (smask is not None and _name(smask) != "None")
            or any(name not in ("Normal", "Compatible") for name in blend_names)
        )
        if transparent:
            self.usage.transparency.append(path + "/ExtGState")
        for key in ("TR", "TR2"):
            transfer = resolve(self.doc, state.get(key)) if state.get(key) is not None else None
            if transfer is not None:
                self.checks.check(
                    _name(transfer) == "Default",
                    "graphics.transfer_function",
                    "ISO 19005 graphics state",
                    f"/{key} shall be absent or /Default.",
                    path + "/" + key,
                )

    def _record_device(self, device: str, resources: PDFDict, path: str) -> None:
        default = _default_space(self.doc, resources, device)
        if default is not None:
            self._validate_device_independent_space(default, resources, path + "/Default" + device)
            return
        self.usage.device_spaces.append((device.upper(), path, resources))

    def _record_color_space(self, value: PDFObject | None, resources: PDFDict, path: str) -> None:
        try:
            value = resolve(self.doc, value)
        except Exception:
            self.checks.fail(
                "color.space_resolve",
                "ISO 32000 color spaces",
                "Color space cannot be resolved.",
                path,
            )
            return
        if value is None:
            return
        if isinstance(value, PDFName):
            name = value.value
            abbreviations = {"G": "Gray", "RGB": "RGB", "CMYK": "CMYK"}
            if name in ("DeviceGray", "DeviceRGB", "DeviceCMYK"):
                self._record_device(name.removeprefix("Device"), resources, path)
                return
            if name in abbreviations:
                self._record_device(abbreviations[name], resources, path)
                return
            if name == "Pattern":
                return
            colors = _dict(self.doc, resources.get("ColorSpace"))
            if colors is None or colors.get(name) is None:
                self.checks.fail(
                    "color.resource_missing",
                    "ISO 32000 color spaces",
                    f"Color-space resource /{name} is not defined.",
                    path,
                )
                return
            self._record_color_space(colors[name], resources, path + "/" + name)
            return
        if not isinstance(value, list) or not value:
            self.checks.fail(
                "color.space_syntax",
                "ISO 32000 color spaces",
                "Color space is neither a name nor a valid color-space array.",
                path,
            )
            return
        family = _name(resolve(self.doc, value[0]))
        if family == "ICCBased":
            self.usage.icc_spaces.append((value, path))
            self._validate_device_independent_space(value, resources, path)
        elif family in ("CalGray", "CalRGB", "Lab"):
            self._validate_device_independent_space(value, resources, path)
        elif family == "Indexed" and len(value) >= 2:
            self._record_color_space(value[1], resources, path + "/Base")
        elif family in ("Separation", "DeviceN"):
            alternate_index = 2 if family == "Separation" else 2
            if len(value) > alternate_index:
                self._record_color_space(value[alternate_index], resources, path + "/Alternate")
        elif family == "Pattern":
            if len(value) > 1:
                self._record_color_space(value[1], resources, path + "/Underlying")
        else:
            self.checks.fail(
                "color.space_family",
                "ISO 32000 color spaces",
                f"Unsupported or malformed color-space family /{family}.",
                path,
            )

    def _validate_device_independent_space(
        self,
        value: PDFObject,
        resources: PDFDict,
        path: str,
    ) -> None:
        try:
            value = resolve(self.doc, value)
        except Exception:
            self.checks.fail(
                "color.space_resolve",
                "ISO 32000 color spaces",
                "Color space cannot be resolved.",
                path,
            )
            return
        if isinstance(value, PDFName):
            if value.value in ("CalGray", "CalRGB", "Lab"):
                self.checks.passed += 1
                return
            self.checks.fail(
                "color.default_device_independent",
                "ISO 19005 color management",
                "Default color spaces shall resolve to a device-independent color space.",
                path,
            )
            return
        if not isinstance(value, list) or not value:
            self.checks.fail(
                "color.default_device_independent",
                "ISO 19005 color management",
                "Default color spaces shall resolve to a device-independent color space.",
                path,
            )
            return
        family = _name(resolve(self.doc, value[0]))
        if family == "ICCBased" and len(value) >= 2:
            profile_stream = _stream(self.doc, value[1])
            if profile_stream is None:
                self.checks.fail(
                    "color.iccbased_profile",
                    "ISO 19005 color management",
                    "ICCBased color space does not reference an ICC stream.",
                    path,
                )
                return
            try:
                profile = parse_icc(decoded_stream_bytes(self.doc, profile_stream))
                declared_n = as_int(resolve(self.doc, profile_stream.get("N")), 0)
                self.checks.check(
                    declared_n == profile.components,
                    "color.iccbased_components",
                    "ISO 19005 color management",
                    f"ICCBased /N {declared_n} does not match ICC components {profile.components}.",
                    path,
                )
                self.checks.check(
                    profile.has_device_to_pcs,
                    "color.icc_forward_mapping",
                    "ISO 19005 color management",
                    "ICCBased profile lacks a usable device-to-PCS mapping.",
                    path,
                )
            except (ICCError, PDFStructureError) as exc:
                self.checks.fail(
                    "color.icc_valid",
                    "ISO 19005 color management",
                    f"ICCBased profile is invalid: {exc}",
                    path,
                )
            return
        if family in ("CalGray", "CalRGB", "Lab"):
            self.checks.passed += 1
            return
        self.checks.fail(
            "color.default_device_independent",
            "ISO 19005 color management",
            f"Color space /{family} is not device-independent for a Default* resource.",
            path,
        )

    def _font_rules(self) -> None:
        seen: set[str] = set()
        for font_value, path in self.usage.fonts:
            font = _dict(self.doc, font_value)
            if font is None:
                self.checks.fail(
                    "fonts.dictionary",
                    "ISO 19005 fonts",
                    "Used font resource is not a font dictionary.",
                    path,
                )
                continue
            identity = str(font_value) if isinstance(font_value, PDFRef) else path
            if identity in seen:
                continue
            seen.add(identity)
            subtype = _name(resolve(self.doc, font.get("Subtype")))
            if subtype == "Type3":
                char_procs = _dict(self.doc, font.get("CharProcs"))
                self.checks.check(
                    bool(char_procs),
                    "fonts.type3_charprocs",
                    "ISO 32000 Type 3 fonts",
                    "Type3 font shall define CharProcs.",
                    path,
                )
                resources = _resource_dict(self.doc, font.get("Resources"))
                if char_procs:
                    for glyph, proc_value in char_procs.items():
                        proc = _stream(self.doc, proc_value)
                        if proc is None:
                            self.checks.fail(
                                "fonts.type3_charproc_stream",
                                "ISO 32000 Type 3 fonts",
                                f"Type3 CharProc /{glyph} is not a stream.",
                                path,
                            )
                            continue
                        try:
                            self._scan_content(
                                decoded_stream_bytes(self.doc, proc),
                                resources,
                                path + "/CharProcs/" + glyph,
                            )
                        except Exception as exc:
                            self.checks.fail(
                                "fonts.type3_content",
                                "ISO 32000 Type 3 fonts",
                                f"Type3 glyph stream cannot be parsed: {exc}",
                                path + "/CharProcs/" + glyph,
                            )
                continue

            descriptor: PDFDict | None = None
            if subtype == "Type0":
                descendants = _array(self.doc, font.get("DescendantFonts")) or []
                self.checks.check(
                    len(descendants) == 1,
                    "fonts.type0_descendant",
                    "ISO 32000 composite fonts",
                    "Type0 font shall contain exactly one descendant CIDFont.",
                    path,
                )
                if descendants:
                    descendant = _dict(self.doc, descendants[0])
                    if descendant:
                        descriptor = _dict(self.doc, descendant.get("FontDescriptor"))
            else:
                descriptor = _dict(self.doc, font.get("FontDescriptor"))
            embedded = bool(
                descriptor
                and any(
                    _stream(self.doc, descriptor.get(key)) is not None
                    for key in ("FontFile", "FontFile2", "FontFile3")
                )
            )
            self.checks.check(
                embedded,
                "fonts.embedded",
                "ISO 19005 fonts",
                "Every font program used to render text shall be embedded.",
                path,
            )

    def _load_output_intents(self) -> None:
        if self.output_intents:
            return
        intents = _array(self.doc, self.doc.catalog.get("OutputIntents")) or []
        fingerprints: set[bytes] = set()
        for index, intent_value in enumerate(intents):
            intent = _dict(self.doc, intent_value)
            if intent is None or _name(resolve(self.doc, intent.get("S"))) != "GTS_PDFA1":
                continue
            path = f"catalog/OutputIntents[{index}]"
            profile_stream = _stream(self.doc, intent.get("DestOutputProfile"))
            if profile_stream is None:
                self.checks.fail(
                    "color.output_intent_profile",
                    "ISO 19005 output intents",
                    "PDF/A OutputIntent shall contain DestOutputProfile.",
                    path,
                )
                continue
            try:
                raw = decoded_stream_bytes(self.doc, profile_stream)
                profile = parse_icc(raw)
            except (ICCError, PDFStructureError) as exc:
                self.checks.fail(
                    "color.output_intent_icc",
                    "ISO 19005 output intents",
                    f"OutputIntent ICC profile is invalid: {exc}",
                    path,
                )
                continue
            self.checks.check(
                profile.profile_class in ("mntr", "prtr"),
                "color.output_intent_class",
                "ISO 19005 output intents",
                f"OutputIntent ICC profile class {profile.profile_class!r} is not display/output.",
                path,
            )
            self.checks.check(
                profile.has_device_to_pcs,
                "color.output_intent_forward_mapping",
                "ISO 19005 output intents",
                "OutputIntent ICC profile lacks a usable device-to-PCS mapping.",
                path,
            )
            fingerprints.add(raw)
            self.output_intents.append(_OutputIntent(profile, path))
        self.checks.check(
            len(fingerprints) <= 1,
            "color.output_intent_consistency",
            "ISO 19005 output intents",
            "Multiple PDF/A OutputIntents shall not contain conflicting destination profiles.",
            "catalog/OutputIntents",
        )

    def _color_rules(self) -> None:
        self._load_output_intents()
        for device, path, _resources in self.usage.device_spaces:
            if device == "RGB":
                compatible = any(intent.profile.color_space == "RGB " for intent in self.output_intents)
            elif device == "CMYK":
                compatible = any(intent.profile.color_space == "CMYK" for intent in self.output_intents)
            else:  # DeviceGray can be rendered through gray, RGB or CMYK output characterization.
                compatible = any(
                    intent.profile.color_space in ("GRAY", "RGB ", "CMYK")
                    for intent in self.output_intents
                )
            self.checks.check(
                compatible,
                "color.unmanaged_device_space",
                "ISO 19005 color management",
                f"Device{device} is used without a matching Default{device} or compatible OutputIntent.",
                path,
            )

    def _attachment_rules(self) -> None:
        unique: list[tuple[PDFDict, str]] = []
        seen: set[str] = set()
        for value, path in self.usage.filespecs:
            filespec = _dict(self.doc, value)
            if filespec is None or filespec.get("EF") is None:
                continue
            identity = str(value) if isinstance(value, PDFRef) else path
            if identity in seen:
                continue
            seen.add(identity)
            unique.append((filespec, path))

        if self.policy.attachment_mode == "none":
            self.checks.check(
                not unique,
                "pdfa1.embedded_files",
                "ISO 19005-1 embedded files",
                "Embedded files are prohibited in PDF/A-1.",
                "document",
            )
            return

        for filespec, path in unique:
            ef = _dict(self.doc, filespec.get("EF"))
            stream: PDFStream | None = None
            if ef:
                stream = _stream(self.doc, ef.get("UF")) or _stream(self.doc, ef.get("F"))
            if not self.checks.check(
                stream is not None,
                "embedded_file.stream",
                "ISO 19005 embedded files",
                "Filespec /EF shall reference an embedded-file stream.",
                path,
            ):
                continue
            assert stream is not None
            self.checks.check(
                _name(resolve(self.doc, stream.get("Type"))) in ("", "EmbeddedFile"),
                "embedded_file.type",
                "ISO 32000 embedded files",
                "Embedded file stream /Type shall be /EmbeddedFile when present.",
                path,
            )
            subtype_name = _name(resolve(self.doc, stream.get("Subtype")))
            mime = subtype_name.replace("#2F", "/").replace("#2f", "/")
            try:
                payload = decoded_stream_bytes(self.doc, stream)
            except Exception as exc:
                self.checks.fail(
                    "embedded_file.decode",
                    "ISO 19005 embedded files",
                    f"Embedded file cannot be decoded: {exc}",
                    path,
                )
                continue

            if self.policy.attachment_mode == "pdfa-or-text":
                if mime.lower() == "text/plain":
                    self.checks.passed += 1
                    continue
                if mime.lower() == "application/pdf" or payload.startswith(b"%PDF-"):
                    self._validate_embedded_pdfa(payload, path)
                    continue
                self.checks.fail(
                    "pdfa2.embedded_file_type",
                    "ISO 19005-2 embedded files",
                    "PDF/A-2 attachments shall be PDF/A documents or plain text.",
                    path,
                )
            else:
                relationship = _name(resolve(self.doc, filespec.get("AFRelationship")))
                self.checks.check(
                    relationship in _ALLOWED_AF_RELATIONSHIPS,
                    "pdfa3.af_relationship",
                    "ISO 19005-3 associated files",
                    "PDF/A-3 embedded files require a valid /AFRelationship.",
                    path,
                )

    def _validate_embedded_pdfa(self, payload: bytes, path: str) -> None:
        if self.depth >= 4:
            self.checks.fail(
                "pdfa2.embedded_recursion",
                "ISO 19005-2 embedded files",
                "Embedded PDF nesting exceeds the native validator safety limit.",
                path,
            )
            return
        try:
            nested = PDFDocument.open(payload, repair=False)
        except Exception as exc:
            self.checks.fail(
                "pdfa2.embedded_pdf_parse",
                "ISO 19005-2 embedded files",
                f"Embedded PDF cannot be parsed: {exc}",
                path,
            )
            return
        metadata = _stream(nested, nested.catalog.get("Metadata"))
        if metadata is None:
            self.checks.fail(
                "pdfa2.embedded_pdfa_metadata",
                "ISO 19005-2 embedded files",
                "Attached PDF lacks PDF/A metadata.",
                path,
            )
            return
        try:
            xmp = XMPDocument.parse(decoded_stream_bytes(nested, metadata))
        except Exception as exc:
            self.checks.fail(
                "pdfa2.embedded_pdfa_metadata",
                "ISO 19005-2 embedded files",
                f"Attached PDF/A metadata is invalid: {exc}",
                path,
            )
            return
        part = xmp.get("pdfaid", "part")
        conf = (xmp.get("pdfaid", "conformance") or "").lower()
        if part not in ("1", "2", "3") or conf not in ("a", "b", "u"):
            self.checks.fail(
                "pdfa2.embedded_pdfa_claim",
                "ISO 19005-2 embedded files",
                "Attached PDF does not claim a recognized PDF/A-1/2/3 conformance.",
                path,
            )
            return
        nested_level = part + "b"  # Level A/U necessarily include the Level-B preservation requirements.
        report = _NativeSession(nested, nested_level, depth=self.depth + 1).run()
        self.checks.check(
            report.compliant,
            "pdfa2.embedded_pdfa_conformance",
            "ISO 19005-2 embedded files",
            "Attached PDF fails native PDF/A baseline validation: "
            + ", ".join(report.failed_rules[:8]),
            path,
        )

    def _form_rules(self) -> None:
        form = _dict(self.doc, self.doc.catalog.get("AcroForm"))
        if form is None:
            return
        need_appearances = resolve(self.doc, form.get("NeedAppearances"))
        self.checks.check(
            need_appearances in (None, False),
            "forms.need_appearances",
            "ISO 19005 interactive forms",
            "/NeedAppearances shall be false or absent.",
            "catalog/AcroForm",
        )
        self.checks.check(
            form.get("XFA") is None,
            "forms.xfa",
            "ISO 19005 interactive forms",
            "XFA forms are prohibited in PDF/A-1/2/3.",
            "catalog/AcroForm/XFA",
        )
        fields = _array(self.doc, form.get("Fields")) or []
        seen: set[str] = set()

        def visit(value: PDFObject, path: str) -> None:
            field = _dict(self.doc, value)
            if field is None:
                self.checks.fail(
                    "forms.field_dictionary",
                    "ISO 32000 forms",
                    "AcroForm field is not a dictionary.",
                    path,
                )
                return
            identity = str(value) if isinstance(value, PDFRef) else path
            if identity in seen:
                return
            seen.add(identity)
            self._action_rules(field, path)
            if _name(resolve(self.doc, field.get("Subtype"))) == "Widget":
                self.checks.check(
                    field.get("AP") is not None,
                    "forms.widget_appearance",
                    "ISO 19005 interactive forms",
                    "Widget annotations shall provide an appearance dictionary.",
                    path,
                )
            kids = _array(self.doc, field.get("Kids")) or []
            for index, kid in enumerate(kids):
                visit(kid, f"{path}/Kids[{index}]")

        for index, field in enumerate(fields):
            visit(field, f"catalog/AcroForm/Fields[{index}]")


class NativePDFAValidator:
    """Validate PDF/A using only source code owned by this repository."""

    def validate(self, source: str | Path | bytes, level: str) -> ValidationReport:
        target = policy(level)
        try:
            doc = PDFDocument.open(source, repair=False)
        except Exception as exc:
            failure = RuleFailure(
                "file.parse",
                "ISO 32000 syntax",
                f"PDF cannot be parsed by the owned engine: {exc}",
                "file",
            )
            return ValidationReport(
                level=target.level,
                compliant=False,
                passed_checks=0,
                failures=(failure,),
            )
        try:
            return _NativeSession(doc, target.level).run()
        except Exception as exc:
            # Validation logic itself must not turn an unevaluated rule into a pass.
            failure = RuleFailure(
                "validator.internal_evaluation",
                "fail-closed validator contract",
                f"Native validator could not complete evaluation: {exc}",
                "document",
            )
            return ValidationReport(
                level=target.level,
                compliant=False,
                passed_checks=0,
                failures=(failure,),
            )
