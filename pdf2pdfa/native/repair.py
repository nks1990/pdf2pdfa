"""Owned PDF/A repair planning and conservative mutations.

This module contains no conversion subprocess and no third-party PDF library.
Repairs are split into operations whose preservation semantics are explicit and
blockers that require the owned font/image/renderer layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .color_profiles import generic_cmyk_profile_bytes, srgb_profile_bytes
from .content import ContentInstruction, InlineImage, parse_content_stream
from .document import PDFDocument, PDFParseError
from .filters import (
    LOSSLESS_GENERAL_FILTERS,
    TERMINAL_IMAGE_FILTERS,
    UnsupportedStreamFilterError,
    decode_pipeline,
    flate_encode,
)
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream, as_int
from .pdfa import NativePDFAValidator, ValidationReport, policy
from .structure import (
    decoded_stream_bytes,
    resolve,
    walk_pages,
    walk_reachable_objects,
)
from .writer import PDFWriter
from .xmp import build_pdfa_xmp


class NativeRepairError(RuntimeError):
    pass


class UnsupportedNativeRepairError(NativeRepairError):
    pass


@dataclass(frozen=True, slots=True)
class RepairBlocker:
    code: str
    message: str
    path: str = ""


@dataclass(slots=True)
class RepairPlan:
    level: str
    operations: list[str] = field(default_factory=list)
    blockers: list[RepairBlocker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def repairable(self) -> bool:
        return not self.blockers

    def block(self, code: str, message: str, path: str = "") -> None:
        key = (code, path, message)
        if key not in {(item.code, item.path, item.message) for item in self.blockers}:
            self.blockers.append(RepairBlocker(code, message, path))


@dataclass(frozen=True, slots=True)
class RepairResult:
    output_path: Path
    level: str
    plan: RepairPlan
    validation: ValidationReport


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


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _resolved_dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _resolved_stream(doc: PDFDocument, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _resolved_array(doc: PDFDocument, value: PDFObject | None) -> list[PDFObject] | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, list) else None


def _filter_names(doc: PDFDocument, stream: PDFStream) -> list[str]:
    try:
        value = resolve(doc, stream.get("Filter"))
    except Exception as exc:
        raise NativeRepairError(f"cannot resolve stream filters: {exc}") from exc
    if value is None:
        return []
    if isinstance(value, PDFName):
        return [value.value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            item = resolve(doc, item)
            if not isinstance(item, PDFName):
                raise NativeRepairError("stream /Filter array contains a non-name")
            result.append(item.value)
        return result
    raise NativeRepairError("stream /Filter is not a name or array")


def _decode_parms(doc: PDFDocument, stream: PDFStream, count: int) -> list[PDFDict | None]:
    value = resolve(doc, stream.get("DecodeParms")) if stream.get("DecodeParms") is not None else None
    if value is None:
        return [None] * count
    if isinstance(value, PDFDict):
        return [value] + [None] * max(0, count - 1)
    if isinstance(value, list):
        output: list[PDFDict | None] = []
        for item in value:
            try:
                item = resolve(doc, item)
            except Exception:
                item = None
            output.append(item if isinstance(item, PDFDict) else None)
        output.extend([None] * max(0, count - len(output)))
        return output[:count]
    return [None] * count


def _info(doc: PDFDocument) -> dict[str, bytes]:
    info = _resolved_dict(doc, doc.trailer.get("Info"))
    if not info:
        return {}
    result: dict[str, bytes] = {}
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
        value = resolve(doc, info.get(key)) if info.get(key) is not None else None
        if isinstance(value, bytes):
            result[key] = value
    return result


def _walk_mutable(value: PDFObject, path: str = "Root") -> Iterator[tuple[PDFDict, str]]:
    seen: set[int] = set()

    def visit(candidate: PDFObject, current: str) -> Iterator[tuple[PDFDict, str]]:
        if isinstance(candidate, PDFStream):
            yield from visit(candidate.dictionary, current + "/dict")
        elif isinstance(candidate, PDFDict):
            identity = id(candidate)
            if identity in seen:
                return
            seen.add(identity)
            yield candidate, current
            for key, child in list(candidate.items()):
                if not isinstance(child, PDFRef):
                    yield from visit(child, current + "/" + key)
        elif isinstance(candidate, list):
            identity = id(candidate)
            if identity in seen:
                return
            seen.add(identity)
            for index, child in enumerate(list(candidate)):
                if not isinstance(child, PDFRef):
                    yield from visit(child, f"{current}[{index}]")

    yield from visit(value, path)


def _action_is_forbidden(doc: PDFDocument, value: PDFObject | None) -> bool:
    action = _resolved_dict(doc, value)
    return bool(action and _name(resolve(doc, action.get("S"))) in _FORBIDDEN_ACTIONS)


def _resource_dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _font_is_embedded(doc: PDFDocument, font_value: PDFObject) -> tuple[bool, str]:
    font = _resolved_dict(doc, font_value)
    if font is None:
        return False, "malformed font dictionary"
    subtype = _name(resolve(doc, font.get("Subtype")))
    if subtype == "Type3":
        return True, subtype
    descriptor = _resolved_dict(doc, font.get("FontDescriptor"))
    if subtype == "Type0":
        descendants = _resolved_array(doc, font.get("DescendantFonts")) or []
        if len(descendants) != 1:
            return False, "Type0 font has invalid DescendantFonts"
        descendant = _resolved_dict(doc, descendants[0])
        descriptor = _resolved_dict(doc, descendant.get("FontDescriptor")) if descendant else None
    embedded = bool(
        descriptor
        and any(
            _resolved_stream(doc, descriptor.get(key)) is not None
            for key in ("FontFile", "FontFile2", "FontFile3")
        )
    )
    return embedded, subtype or "unknown"


class NativeRepairEngine:
    """Conservative PDF/A repair using only the owned native engine."""

    def __init__(self, *, allow_attachment_removal: bool = False) -> None:
        self.allow_attachment_removal = allow_attachment_removal

    def plan(self, source: str | Path | bytes, level: str) -> RepairPlan:
        target = policy(level)
        plan = RepairPlan(target.level)
        try:
            doc = PDFDocument.open(source, repair=True)
        except Exception as exc:
            plan.block("file.parse", f"owned parser cannot open input: {exc}", "file")
            return plan

        if "Encrypt" in doc.trailer:
            plan.block(
                "encryption",
                "encrypted input requires the owned Standard Security Handler, which must decrypt before repair",
                "trailer/Encrypt",
            )

        initial = NativePDFAValidator().validate(source, target.level)
        if initial.compliant:
            plan.operations.append("passthrough already-conforming PDF/A")
            return plan

        failures = initial.failures
        failure_rules = {failure.rule_id for failure in failures}

        if "pdfa1.transparency" in failure_rules:
            plan.block(
                "pdfa1.transparency",
                "PDF/A-1 transparency requires native appearance flattening",
            )
        if any(rule.startswith("fonts.") for rule in failure_rules):
            # Missing resources/malformed Type0 mappings cannot be guessed by a
            # dictionary-only repair. The owned font engine resolves these.
            for failure in failures:
                if failure.rule_id in {
                    "fonts.embedded",
                    "fonts.resource_missing",
                    "fonts.dictionary",
                    "fonts.type0_descendant",
                }:
                    plan.block(
                        failure.rule_id,
                        "font repair requires the owned font engine: " + failure.message,
                        failure.path,
                    )
        if "annotations.dynamic" in failure_rules:
            plan.block(
                "annotations.dynamic",
                "dynamic annotation removal requires native appearance flattening to preserve static appearance",
            )
        if "forms.widget_appearance" in failure_rules:
            plan.block(
                "forms.widget_appearance",
                "widget without an appearance stream requires native form appearance generation",
            )

        for failure in failures:
            if failure.rule_id == "pdfa2.embedded_file_type" and not self.allow_attachment_removal:
                plan.block(
                    "pdfa2.embedded_file_type",
                    "non-PDF/A/non-text attachment cannot be preserved in PDF/A-2; use PDF/A-3 or explicitly allow removal",
                    failure.path,
                )

        safe_rule_prefixes = {
            "metadata.",
            "actions.",
            "stream.lzw",
            "stream.crypt_filter",
            "stream.external_data",
            "color.",
            "pdfa1.xref_stream",
            "pdfa1.object_stream",
            "pdfa1.pdf_version",
            "pdfa23.pdf_version",
            "forms.need_appearances",
            "forms.xfa",
            "annotations.visibility",
            "pdfa1.embedded_files",
            "pdfa1.file_attachment_annotation",
            "pdfa2.embedded_file_type",
            "pdfa3.af_relationship",
            "pdfa1.optional_content",
        }
        if any(
            any(failure.rule_id.startswith(prefix) for prefix in safe_rule_prefixes)
            for failure in failures
        ):
            plan.operations.append("native structural/PDF-A normalization")
        if not plan.blockers:
            plan.operations.append("native validation gate and deterministic rewrite")
        return plan

    def repair_document(self, doc: PDFDocument, level: str, plan: RepairPlan) -> None:
        target = policy(level)
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "native repair blocked: "
                + "; ".join(f"{item.code}: {item.message}" for item in plan.blockers)
            )

        self._normalize_stream_filters(doc, plan)
        self._remove_forbidden_actions(doc, plan)
        self._normalize_forms(doc, plan)
        self._normalize_annotations(doc, target.part, plan)
        self._normalize_attachments(doc, target.part, plan)
        self._normalize_optional_content(doc, target.part, plan)
        self._normalize_color(doc, plan)
        self._normalize_metadata(doc, target.part, target.conformance, plan)
        doc.trailer.pop("Encrypt", None)
        doc.trailer.pop("Prev", None)
        doc.trailer.pop("XRefStm", None)

    def convert(
        self,
        source: str | Path | bytes,
        destination: str | Path,
        level: str,
    ) -> RepairResult:
        target = policy(level)
        plan = self.plan(source, target.level)
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "native repair blockers: "
                + "; ".join(f"{item.code}: {item.message}" for item in plan.blockers)
            )
        doc = PDFDocument.open(source, repair=True)
        self.repair_document(doc, target.level, plan)
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        candidate = destination.with_name(destination.name + ".pdf2pdfa-native.tmp")
        try:
            PDFWriter(
                doc,
                version="1.4" if target.part == 1 else "1.7",
                reachable_only=True,
            ).write(candidate)
            validation = NativePDFAValidator().validate(candidate, target.level)
            if not validation.compliant:
                raise NativeRepairError(
                    "owned repair candidate failed owned validation: "
                    + ", ".join(validation.failed_rules)
                )
            candidate.replace(destination)
        finally:
            candidate.unlink(missing_ok=True)
        return RepairResult(destination, target.level, plan, validation)

    def _normalize_stream_filters(self, doc: PDFDocument, plan: RepairPlan) -> None:
        changed = 0
        for _path, value in walk_reachable_objects(doc):
            if not isinstance(value, PDFStream):
                continue
            filters = _filter_names(doc, value)
            if not filters:
                continue
            if "Crypt" in filters:
                raise UnsupportedNativeRepairError(
                    "Crypt filter stream requires owned encryption handler before normalization"
                )
            if not any(name in ("LZWDecode", "LZW") for name in filters):
                continue
            if any(name in TERMINAL_IMAGE_FILTERS for name in filters):
                raise UnsupportedNativeRepairError(
                    "LZW combined with a terminal image codec requires native image decode/re-encode"
                )
            parms = _decode_parms(doc, value, len(filters))
            decoded = decode_pipeline(value.data, filters, parms)
            value.data = flate_encode(decoded)
            value.dictionary["Filter"] = PDFName("FlateDecode")
            value.dictionary.pop("DecodeParms", None)
            value.dictionary["Length"] = len(value.data)
            changed += 1
        if changed:
            plan.operations.append(f"normalized {changed} LZW stream(s) to Flate")

    def _remove_forbidden_actions(self, doc: PDFDocument, plan: RepairPlan) -> None:
        changed = 0
        catalog = doc.catalog
        if _action_is_forbidden(doc, catalog.get("OpenAction")):
            catalog.pop("OpenAction", None)
            changed += 1
        names = _resolved_dict(doc, catalog.get("Names"))
        if names and names.get("JavaScript") is not None:
            names.pop("JavaScript", None)
            changed += 1

        for _path, value in walk_reachable_objects(doc):
            dictionary: PDFDict | None = None
            if isinstance(value, PDFStream):
                dictionary = value.dictionary
            elif isinstance(value, PDFDict):
                dictionary = value
            if dictionary is None:
                continue
            if _action_is_forbidden(doc, dictionary.get("A")):
                dictionary.pop("A", None)
                changed += 1
            aa = _resolved_dict(doc, dictionary.get("AA"))
            if aa:
                for event, action in list(aa.items()):
                    if _action_is_forbidden(doc, action):
                        aa.pop(event, None)
                        changed += 1
                if not aa:
                    dictionary.pop("AA", None)
        if changed:
            plan.operations.append(f"removed {changed} prohibited action reference(s)")

    def _normalize_forms(self, doc: PDFDocument, plan: RepairPlan) -> None:
        form = _resolved_dict(doc, doc.catalog.get("AcroForm"))
        if not form:
            return
        changed = 0
        if form.get("NeedAppearances") not in (None, False):
            form["NeedAppearances"] = False
            changed += 1
        if form.get("XFA") is not None:
            form.pop("XFA", None)
            changed += 1
        if changed:
            plan.operations.append("normalized AcroForm dynamic state")

    def _normalize_annotations(self, doc: PDFDocument, part: int, plan: RepairPlan) -> None:
        changed = 0
        for page in walk_pages(doc):
            annots = _resolved_array(doc, page.dictionary.get("Annots"))
            if not annots:
                continue
            kept: list[PDFObject] = []
            for value in annots:
                annot = _resolved_dict(doc, value)
                if annot is None:
                    kept.append(value)
                    continue
                subtype = _name(resolve(doc, annot.get("Subtype")))
                if part == 1 and subtype == "FileAttachment":
                    changed += 1
                    continue
                if subtype and subtype != "Popup":
                    flags = as_int(resolve(doc, annot.get("F")) if annot.get("F") is not None else 0, 0)
                    flags |= 4  # Print
                    flags &= ~1  # Invisible
                    flags &= ~2  # Hidden
                    flags &= ~32  # NoView
                    if flags != as_int(resolve(doc, annot.get("F")) if annot.get("F") is not None else 0, 0):
                        annot["F"] = flags
                        changed += 1
                kept.append(value)
            if kept != annots:
                page.dictionary["Annots"] = kept
        if changed:
            plan.operations.append(f"normalized/removed {changed} annotation property instance(s)")

    def _normalize_attachments(self, doc: PDFDocument, part: int, plan: RepairPlan) -> None:
        catalog = doc.catalog
        if part == 1:
            names = _resolved_dict(doc, catalog.get("Names"))
            if names and names.get("EmbeddedFiles") is not None:
                names.pop("EmbeddedFiles", None)
                plan.operations.append("removed PDF/A-1 embedded-file name tree")
            if catalog.get("AF") is not None:
                catalog.pop("AF", None)
                plan.operations.append("removed PDF/A-1 associated-file array")
            return

        filespecs: list[PDFRef] = []
        names = _resolved_dict(doc, catalog.get("Names"))
        embedded_tree = _resolved_dict(doc, names.get("EmbeddedFiles")) if names else None
        if embedded_tree:
            pairs = _resolved_array(doc, embedded_tree.get("Names"))
            if pairs:
                new_pairs: list[PDFObject] = []
                for index in range(0, len(pairs) - 1, 2):
                    key, value = pairs[index], pairs[index + 1]
                    spec = _resolved_dict(doc, value)
                    keep = True
                    if part == 2 and spec:
                        ef = _resolved_dict(doc, spec.get("EF"))
                        stream = _resolved_stream(doc, ef.get("UF")) if ef else None
                        stream = stream or (_resolved_stream(doc, ef.get("F")) if ef else None)
                        if stream:
                            mime = _name(resolve(doc, stream.get("Subtype"))).lower()
                            if mime not in ("text/plain", "application/pdf"):
                                keep = False
                    if keep:
                        new_pairs.extend([key, value])
                        if isinstance(value, PDFRef):
                            filespecs.append(value)
                    elif self.allow_attachment_removal:
                        plan.warnings.append("removed attachment incompatible with PDF/A-2")
                    else:
                        raise UnsupportedNativeRepairError(
                            "PDF/A-2 incompatible attachment encountered without allow_attachment_removal"
                        )
                embedded_tree["Names"] = new_pairs

        if part == 3:
            # Ensure every reachable embedded filespec is associated with a
            # defined relationship and the Catalog /AF array.
            for _path, value in walk_reachable_objects(doc):
                spec = value if isinstance(value, PDFDict) else None
                if not spec or _name(spec.get("Type")) != "Filespec" or spec.get("EF") is None:
                    continue
                if not _name(resolve(doc, spec.get("AFRelationship"))):
                    spec["AFRelationship"] = PDFName("Unspecified")
                # Only indirect filespecs can be put safely in Catalog /AF.
            af = _resolved_array(doc, catalog.get("AF")) or []
            existing = {str(item) for item in af if isinstance(item, PDFRef)}
            for ref in filespecs:
                if str(ref) not in existing:
                    af.append(ref)
                    existing.add(str(ref))
            if af:
                catalog["AF"] = af

    def _normalize_optional_content(self, doc: PDFDocument, part: int, plan: RepairPlan) -> None:
        if part == 1 and doc.catalog.get("OCProperties") is not None:
            # Removing the catalog registration does not remove marked-content
            # operators; they render as ordinary content in PDF 1.4.
            doc.catalog.pop("OCProperties", None)
            plan.operations.append("removed PDF/A-1 optional-content catalog registration")

    def _detect_device_cmyk(self, doc: PDFDocument) -> bool:
        for page in walk_pages(doc):
            try:
                content = decoded_stream_bytes(doc, page.dictionary.get("Contents"))
            except Exception:
                # Page contents may be an array; use validator's conservative
                # result later rather than claiming no CMYK.
                continue
            try:
                for item in parse_content_stream(content):
                    if isinstance(item, ContentInstruction) and item.operator in ("k", "K"):
                        return True
            except Exception:
                continue
        for _path, value in walk_reachable_objects(doc):
            dictionary = value.dictionary if isinstance(value, PDFStream) else value if isinstance(value, PDFDict) else None
            if dictionary and _name(resolve(doc, dictionary.get("ColorSpace"))) == "DeviceCMYK":
                return True
        return False

    def _ensure_colorspace_dict(self, doc: PDFDocument, resources: PDFDict) -> PDFDict:
        value = resources.get("ColorSpace")
        existing = _resolved_dict(doc, value)
        if existing is not None:
            return existing
        created = PDFDict()
        resources["ColorSpace"] = created
        return created

    def _normalize_color(self, doc: PDFDocument, plan: RepairPlan) -> None:
        uses_cmyk = self._detect_device_cmyk(doc)
        output_bytes = generic_cmyk_profile_bytes() if uses_cmyk else srgb_profile_bytes()
        components = 4 if uses_cmyk else 3
        output_stream = PDFStream(PDFDict({"N": components}), output_bytes)
        output_ref = doc.new_object(output_stream)
        intent = PDFDict(
            {
                "Type": PDFName("OutputIntent"),
                "S": PDFName("GTS_PDFA1"),
                "OutputConditionIdentifier": (
                    b"pdf2pdfa generic CMYK" if uses_cmyk else b"pdf2pdfa sRGB"
                ),
                "Info": b"pdf2pdfa owned color characterization",
                "DestOutputProfile": output_ref,
            }
        )
        doc.catalog["OutputIntents"] = [intent]

        # If CMYK defines the OutputIntent, DeviceRGB still needs an independent
        # characterization. Install our generated sRGB as DefaultRGB on page and
        # nested resource dictionaries. When RGB is the OutputIntent, no such
        # override is needed.
        srgb_ref: PDFRef | None = None
        if uses_cmyk:
            srgb_ref = doc.new_object(PDFStream(PDFDict({"N": 3}), srgb_profile_bytes()))
        touched = 0
        for page in walk_pages(doc):
            resources = page.resources
            if uses_cmyk and srgb_ref is not None:
                colors = self._ensure_colorspace_dict(doc, resources)
                colors["DefaultRGB"] = [PDFName("ICCBased"), srgb_ref]
                touched += 1
        plan.operations.append(
            "embedded owned CMYK OutputIntent" if uses_cmyk else "embedded owned sRGB OutputIntent"
        )
        if touched:
            plan.operations.append(f"installed DefaultRGB in {touched} page resource dictionary(ies)")

    def _normalize_metadata(
        self,
        doc: PDFDocument,
        part: int,
        conformance: str,
        plan: RepairPlan,
    ) -> None:
        existing = _resolved_stream(doc, doc.catalog.get("Metadata"))
        existing_bytes: bytes | None = None
        if existing is not None:
            try:
                existing_bytes = decoded_stream_bytes(doc, existing)
            except Exception:
                existing_bytes = None
        xmp = build_pdfa_xmp(
            part=part,
            conformance=conformance,
            info=_info(doc),
            producer="pdf2pdfa owned engine",
            existing=existing_bytes,
            now=datetime.now(timezone.utc),
        )
        metadata = PDFStream(
            PDFDict({"Type": PDFName("Metadata"), "Subtype": PDFName("XML")}),
            xmp,
        )
        metadata_ref = doc.new_object(metadata)
        doc.catalog["Metadata"] = metadata_ref
        info = _resolved_dict(doc, doc.trailer.get("Info"))
        if info is not None:
            info["Producer"] = b"pdf2pdfa owned engine"
        plan.operations.append("synchronized PDF/A XMP metadata")
