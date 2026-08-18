"""Native PDF/A-1b, PDF/A-2b and PDF/A-3b validation.

The validator is intentionally self-contained at runtime: it does not invoke
veraPDF, Ghostscript or any other external executable.  pikepdf is used only as
our low-level PDF object parser; PDF/A policy and conformance decisions are
implemented here.

The implementation is fail-closed.  If a structure that affects conformance
cannot be parsed or classified, validation records a failure instead of
silently treating the document as conforming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import pikepdf
from pikepdf import Name, Pdf

from .profiles import get_policy


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    rule_id: str
    clause: str
    message: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    compliant: bool
    flavour: str
    failed_checks: int = 0
    passed_checks: int = 0
    failed_rules: tuple[str, ...] = field(default_factory=tuple)
    failures: tuple[ValidationFailure, ...] = field(default_factory=tuple)
    validator: str = "pdf2pdfa-native"


class ValidationExecutionError(RuntimeError):
    pass


class _Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[ValidationFailure] = []

    def check(
        self,
        condition: bool,
        rule_id: str,
        clause: str,
        message: str,
        path: str = "",
    ) -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(
                ValidationFailure(
                    rule_id=rule_id,
                    clause=clause,
                    message=message,
                    path=path,
                )
            )

    def fail(self, rule_id: str, clause: str, message: str, path: str = "") -> None:
        self.check(False, rule_id, clause, message, path)


_FORBIDDEN_ACTIONS = {
    "/JavaScript",
    "/Launch",
    "/Sound",
    "/Movie",
    "/ResetForm",
    "/ImportData",
    "/Hide",
    "/Rendition",
    "/Trans",
    "/GoTo3DView",
    # Deprecated PDF action types.
    "/SetState",
    "/NoOp",
}

_DYNAMIC_ANNOTATIONS = {
    "/Sound",
    "/Movie",
    "/Screen",
    "/3D",
    "/RichMedia",
}

_STANDARD_XMP_NAMESPACES = {
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "adobe:ns:meta/",
    "http://purl.org/dc/elements/1.1/",
    "http://ns.adobe.com/xap/1.0/",
    "http://ns.adobe.com/pdf/1.3/",
    "http://ns.adobe.com/xap/1.0/mm/",
    "http://ns.adobe.com/xap/1.0/rights/",
    "http://ns.adobe.com/photoshop/1.0/",
    "http://www.aiim.org/pdfa/ns/id/",
    "http://www.aiim.org/pdfa/ns/extension/",
    "http://www.aiim.org/pdfa/ns/schema#",
    "http://www.aiim.org/pdfa/ns/property#",
    "http://www.aiim.org/pdfa/ns/type#",
    "http://www.aiim.org/pdfa/ns/field#",
}

_ALLOWED_AF_RELATIONSHIPS = {
    "/Source",
    "/Data",
    "/Alternative",
    "/Supplement",
    "/Unspecified",
}

_PDF_NAME_ESCAPE = re.compile(r"#([0-9A-Fa-f]{2})")


def _name(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def _dict_get(obj: Any, key: str, default: Any = None) -> Any:
    try:
        return obj.get(key, default)
    except Exception:
        return default


def _object_path(obj: Any, fallback: str) -> str:
    try:
        objgen = obj.objgen
        if objgen != (0, 0):
            return f"obj {objgen[0]} {objgen[1]}"
    except Exception:
        pass
    return fallback


def _filters(stream: Any) -> list[str]:
    value = _dict_get(stream, "/Filter")
    if value is None:
        return []
    if isinstance(value, pikepdf.Array):
        return [_name(item) for item in value]
    return [_name(value)]


def _decode_pdf_name(value: Any) -> str:
    text = _name(value).lstrip("/")
    return _PDF_NAME_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _is_font_embedded(font: Any) -> bool:
    subtype = _name(_dict_get(font, "/Subtype"))
    if subtype == "/Type3":
        return True
    descriptor = _dict_get(font, "/FontDescriptor")
    if subtype == "/Type0":
        descendants = _dict_get(font, "/DescendantFonts") or []
        if descendants:
            descriptor = _dict_get(descendants[0], "/FontDescriptor")
    return bool(
        descriptor
        and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
    )


def _icc_info(data: bytes) -> tuple[str, str, set[str]]:
    if len(data) < 132:
        raise ValueError("ICC profile is shorter than the mandatory header and tag table")
    declared = int.from_bytes(data[0:4], "big")
    if declared and declared > len(data):
        raise ValueError("ICC declared size exceeds embedded profile size")
    if data[36:40] != b"acsp":
        raise ValueError("ICC profile is missing the acsp signature")
    profile_class = data[12:16].decode("ascii", "replace")
    color_space = data[16:20].decode("ascii", "replace")
    count = int.from_bytes(data[128:132], "big")
    if count > 4096 or 132 + count * 12 > len(data):
        raise ValueError("ICC profile has an invalid tag table")
    tags: set[str] = set()
    for index in range(count):
        off = 132 + index * 12
        sig = data[off : off + 4].decode("ascii", "replace")
        tag_off = int.from_bytes(data[off + 4 : off + 8], "big")
        tag_len = int.from_bytes(data[off + 8 : off + 12], "big")
        if tag_off + tag_len > len(data):
            raise ValueError(f"ICC tag {sig!r} points outside the profile")
        tags.add(sig)
    return profile_class, color_space, tags


def _iter_fields(fields: Iterable[Any]) -> Iterable[Any]:
    for field in fields:
        yield field
        kids = _dict_get(field, "/Kids") or []
        yield from _iter_fields(kids)


def _iter_name_tree(tree: Any) -> Iterable[tuple[Any, Any]]:
    if not tree:
        return
    names = _dict_get(tree, "/Names") or []
    for index in range(0, len(names) - 1, 2):
        yield names[index], names[index + 1]
    for kid in _dict_get(tree, "/Kids") or []:
        yield from _iter_name_tree(kid)


def _metadata_value(meta: Any, key: str) -> str:
    try:
        value = meta.get(key)
    except Exception:
        return ""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    return str(value)


def _xml_namespaces(xml_data: bytes) -> tuple[set[str], bool]:
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise ValueError(f"XMP is not well-formed XML: {exc}") from exc
    namespaces: set[str] = set()
    extension_declared = False
    for element in root.iter():
        if element.tag.startswith("{"):
            uri = element.tag[1:].split("}", 1)[0]
            namespaces.add(uri)
            if uri == "http://www.aiim.org/pdfa/ns/extension/":
                extension_declared = True
        for attr in element.attrib:
            if attr.startswith("{"):
                namespaces.add(attr[1:].split("}", 1)[0])
    return namespaces, extension_declared


class _NativeValidationSession:
    def __init__(self, pdf: Pdf, flavour: str, *, recursion_depth: int = 0) -> None:
        self.pdf = pdf
        self.flavour = flavour.lower()
        self.policy = get_policy(self.flavour)
        self.part = self.policy.part
        self.checks = _Checks()
        self.recursion_depth = recursion_depth
        self.used_device_spaces: set[str] = set()
        self.used_fonts: list[tuple[Any, str]] = []
        self.filespecs: list[tuple[Any, str]] = []
        self._seen_content: set[tuple[int, int] | str] = set()

    def run(self) -> ValidationResult:
        self._file_structure()
        self._metadata()
        self._document_objects()
        self._pages_and_content()
        self._fonts()
        self._color()
        self._embedded_files()
        self._forms()
        failures = tuple(self.checks.failures)
        return ValidationResult(
            compliant=not failures,
            flavour=self.flavour,
            failed_checks=len(failures),
            passed_checks=self.checks.passed,
            failed_rules=tuple(dict.fromkeys(item.rule_id for item in failures)),
            failures=failures,
        )

    def _file_structure(self) -> None:
        self.checks.check(
            not bool(getattr(self.pdf, "is_encrypted", False)),
            "file.encryption",
            "6.1",
            "PDF/A files shall not be encrypted.",
            "trailer",
        )
        try:
            version = tuple(int(part) for part in str(self.pdf.pdf_version).split(".")[:2])
        except Exception:
            version = (99, 99)
        if self.part in (2, 3):
            self.checks.check(
                version <= (1, 7),
                "file.pdf_version",
                "6.1",
                f"PDF/A-{self.part} is based on PDF 1.7; found PDF {self.pdf.pdf_version}.",
                "header",
            )

        for obj in self.pdf.objects:
            path = _object_path(obj, "object")
            if isinstance(obj, pikepdf.Stream):
                filters = _filters(obj)
                self.checks.check(
                    "/LZWDecode" not in filters and "/LZW" not in filters,
                    "stream.lzw",
                    "6.1",
                    "LZW compression is prohibited in PDF/A.",
                    path,
                )
                self.checks.check(
                    "/Crypt" not in filters,
                    "stream.crypt_filter",
                    "6.1",
                    "Crypt filters are prohibited in PDF/A.",
                    path,
                )
                self.checks.check(
                    not any(key in obj for key in ("/F", "/FFilter", "/FDecodeParms")),
                    "stream.external_data",
                    "6.1",
                    "External stream data is prohibited; stream bytes must be embedded.",
                    path,
                )
                if self.part == 1:
                    self.checks.check(
                        "/JPXDecode" not in filters,
                        "pdfa1.jpeg2000",
                        "6.1",
                        "JPEG 2000 / JPXDecode is not part of the PDF/A-1 feature set.",
                        path,
                    )
            if hasattr(obj, "get"):
                obj_type = _name(_dict_get(obj, "/Type"))
                if self.part == 1:
                    self.checks.check(
                        obj_type not in ("/ObjStm", "/XRef"),
                        "pdfa1.object_xref_streams",
                        "6.1",
                        "PDF/A-1 shall not use object streams or cross-reference streams.",
                        path,
                    )

    def _metadata(self) -> None:
        root = self.pdf.Root
        metadata = _dict_get(root, "/Metadata")
        self.checks.check(
            metadata is not None,
            "metadata.required",
            "6.7",
            "A PDF/A document requires document-level XMP metadata.",
            "catalog",
        )
        if metadata is None:
            return
        path = _object_path(metadata, "catalog/Metadata")
        self.checks.check(
            isinstance(metadata, pikepdf.Stream),
            "metadata.stream",
            "6.7",
            "Catalog Metadata shall be a metadata stream.",
            path,
        )
        if not isinstance(metadata, pikepdf.Stream):
            return
        self.checks.check(
            _name(_dict_get(metadata, "/Type")) in ("", "/Metadata"),
            "metadata.type",
            "6.7",
            "XMP metadata stream Type shall be /Metadata when present.",
            path,
        )
        self.checks.check(
            _name(_dict_get(metadata, "/Subtype")) == "/XML",
            "metadata.subtype",
            "6.7",
            "XMP metadata stream Subtype shall be /XML.",
            path,
        )
        try:
            raw = metadata.read_bytes()
            namespaces, extension_declared = _xml_namespaces(raw)
        except Exception as exc:
            self.checks.fail(
                "metadata.xml",
                "6.7",
                f"Could not parse XMP metadata: {exc}",
                path,
            )
            return

        custom = {
            uri
            for uri in namespaces
            if uri not in _STANDARD_XMP_NAMESPACES
            and not uri.startswith("http://www.w3.org/XML/")
        }
        self.checks.check(
            not custom or extension_declared,
            "metadata.extension_schema",
            "6.7",
            "Custom XMP namespaces require a PDF/A extension-schema declaration: "
            + ", ".join(sorted(custom)),
            path,
        )

        try:
            with self.pdf.open_metadata(
                set_pikepdf_as_editor=False,
                update_docinfo=False,
                strict=True,
            ) as meta:
                part = _metadata_value(meta, "pdfaid:part")
                conf = _metadata_value(meta, "pdfaid:conformance").upper()
                fmt = _metadata_value(meta, "dc:format")
                self.checks.check(
                    part == str(self.part),
                    "metadata.pdfaid_part",
                    "6.7",
                    f"pdfaid:part shall be {self.part}; found {part!r}.",
                    path,
                )
                self.checks.check(
                    conf == self.flavour[1].upper(),
                    "metadata.pdfaid_conformance",
                    "6.7",
                    f"pdfaid:conformance shall be {self.flavour[1].upper()}; found {conf!r}.",
                    path,
                )
                self.checks.check(
                    fmt == "application/pdf",
                    "metadata.dc_format",
                    "6.7",
                    "dc:format shall be 'application/pdf'.",
                    path,
                )
                self._metadata_info_sync(meta, path)
        except Exception as exc:
            self.checks.fail(
                "metadata.model",
                "6.7",
                f"XMP metadata could not be interpreted strictly: {exc}",
                path,
            )

    def _metadata_info_sync(self, meta: Any, path: str) -> None:
        info = self.pdf.docinfo
        mapping = {
            "/Title": "dc:title",
            "/Author": "dc:creator",
            "/Subject": "dc:description",
            "/Keywords": "pdf:Keywords",
            "/Creator": "xmp:CreatorTool",
            "/Producer": "pdf:Producer",
        }
        for info_key, xmp_key in mapping.items():
            if info_key not in info:
                continue
            info_value = str(info.get(info_key, "")).strip()
            xmp_value = _metadata_value(meta, xmp_key).strip()
            if not info_value:
                continue
            # dc:creator may be an array; containment is the least lossy stable
            # comparison pikepdf exposes across XMP encodings.
            self.checks.check(
                info_value == xmp_value or info_value in xmp_value,
                "metadata.info_sync",
                "6.7",
                f"DocumentInfo {info_key} is not synchronized with XMP {xmp_key}.",
                path,
            )

    def _document_objects(self) -> None:
        root = self.pdf.Root
        if self.part == 1:
            self.checks.check(
                _dict_get(root, "/OCProperties") is None,
                "pdfa1.optional_content",
                "6.1",
                "Optional-content groups/layers are not supported by PDF/A-1.",
                "catalog",
            )

        for obj in self.pdf.objects:
            if not hasattr(obj, "get"):
                continue
            path = _object_path(obj, "object")
            obj_type = _name(_dict_get(obj, "/Type"))
            subtype = _name(_dict_get(obj, "/Subtype"))
            if obj_type == "/Filespec" or _dict_get(obj, "/EF") is not None:
                self.filespecs.append((obj, path))
            if obj_type == "/XObject" and subtype == "/PS":
                self.checks.fail(
                    "graphics.postscript_xobject",
                    "6.2",
                    "PostScript XObjects are prohibited in PDF/A.",
                    path,
                )
            if subtype == "/Image":
                self.checks.check(
                    _dict_get(obj, "/Alternates") is None,
                    "graphics.alternate_images",
                    "6.2",
                    "Alternate images are prohibited in PDF/A.",
                    path,
                )
            for key in ("/OPI", "/Ref"):
                if key in obj:
                    self.checks.fail(
                        "graphics.external_reference",
                        "6.2",
                        f"{key} introduces an external rendering dependency.",
                        path,
                    )
            for key in ("/TR", "/TR2"):
                value = _dict_get(obj, key)
                if value is not None:
                    self.checks.check(
                        _name(value) == "/Default",
                        "graphics.transfer_function",
                        "6.2",
                        f"{key} transfer functions shall be absent or /Default.",
                        path,
                    )
            self._check_action_dictionary(obj, path)

    def _check_action_dictionary(self, obj: Any, path: str) -> None:
        action_type = _name(_dict_get(obj, "/S"))
        if action_type in _FORBIDDEN_ACTIONS:
            self.checks.fail(
                "actions.forbidden_type",
                "6.6",
                f"Action type {action_type} is prohibited in PDF/A-{self.part}.",
                path,
            )
        aa = _dict_get(obj, "/AA")
        if aa:
            for key, action in aa.items():
                action_name = _name(_dict_get(action, "/S"))
                if action_name in _FORBIDDEN_ACTIONS:
                    self.checks.fail(
                        "actions.forbidden_additional_action",
                        "6.6",
                        f"Additional action {key} uses prohibited action {action_name}.",
                        path,
                    )

    def _pages_and_content(self) -> None:
        for page_index, page in enumerate(self.pdf.pages, start=1):
            path = f"page {page_index}"
            page_obj = page.obj
            annots = _dict_get(page_obj, "/Annots") or []
            for annot_index, annot in enumerate(annots, start=1):
                self._annotation(annot, f"{path}/Annot[{annot_index}]")
            self._scan_content(page, _dict_get(page_obj, "/Resources"), path)

    def _annotation(self, annot: Any, path: str) -> None:
        subtype = _name(_dict_get(annot, "/Subtype"))
        self.checks.check(
            subtype not in _DYNAMIC_ANNOTATIONS,
            "annotations.dynamic",
            "6.5",
            f"Dynamic annotation subtype {subtype} is prohibited.",
            path,
        )
        flags = int(_dict_get(annot, "/F", 0) or 0)
        invisible = bool(flags & 1)
        hidden = bool(flags & 2)
        print_flag = bool(flags & 4)
        no_view = bool(flags & 32)
        if subtype not in ("/Popup", ""):
            self.checks.check(
                print_flag and not invisible and not hidden and not no_view,
                "annotations.visibility",
                "6.5",
                "Annotations shall be printable and not hidden, invisible or NoView.",
                path,
            )
        if subtype == "/FileAttachment" and self.part == 1:
            self.checks.fail(
                "pdfa1.file_attachment_annotation",
                "6.8",
                "File attachments are prohibited in PDF/A-1.",
                path,
            )
        self._check_action_dictionary(annot, path)
        appearance = _dict_get(annot, "/AP")
        if appearance:
            for appearance_name, candidate in appearance.items():
                if hasattr(candidate, "get"):
                    self._scan_content(
                        candidate,
                        _dict_get(candidate, "/Resources"),
                        f"{path}/AP{appearance_name}",
                    )

    def _content_identity(self, target: Any, path: str) -> tuple[int, int] | str:
        obj = getattr(target, "obj", target)
        try:
            objgen = obj.objgen
            if objgen != (0, 0):
                return objgen
        except Exception:
            pass
        return path

    def _scan_content(self, target: Any, resources: Any, path: str) -> None:
        identity = self._content_identity(target, path)
        if identity in self._seen_content:
            return
        self._seen_content.add(identity)

        self._scan_resource_defaults(resources, path)
        try:
            instructions = pikepdf.parse_content_stream(target)
        except Exception as exc:
            self.checks.fail(
                "content.parse",
                "6.2",
                f"Content stream could not be parsed: {exc}",
                path,
            )
            return

        for instruction in instructions:
            operator = _name(getattr(instruction, "operator", ""))
            operands = list(getattr(instruction, "operands", []) or [])
            if operator in ("rg", "RG"):
                if not self._has_default_space(resources, "/DefaultRGB"):
                    self.used_device_spaces.add("RGB")
            elif operator in ("k", "K"):
                if not self._has_default_space(resources, "/DefaultCMYK"):
                    self.used_device_spaces.add("CMYK")
            elif operator in ("g", "G"):
                if not self._has_default_space(resources, "/DefaultGray"):
                    self.used_device_spaces.add("GRAY")
            elif operator in ("cs", "CS") and operands:
                cs = _name(operands[-1])
                if cs == "/DeviceRGB" and not self._has_default_space(resources, "/DefaultRGB"):
                    self.used_device_spaces.add("RGB")
                elif cs == "/DeviceCMYK" and not self._has_default_space(resources, "/DefaultCMYK"):
                    self.used_device_spaces.add("CMYK")
                elif cs == "/DeviceGray" and not self._has_default_space(resources, "/DefaultGray"):
                    self.used_device_spaces.add("GRAY")
            elif operator == "Tf" and operands:
                font_name = _name(operands[0])
                fonts = _dict_get(resources, "/Font") if resources else None
                font = _dict_get(fonts, font_name) if fonts else None
                if font is None:
                    self.checks.fail(
                        "fonts.resource_missing",
                        "6.3",
                        f"Content selects font resource {font_name} but it is not defined.",
                        path,
                    )
                else:
                    self.used_fonts.append((font, f"{path}/Font{font_name}"))
            elif operator == "Do" and operands and resources:
                xobjects = _dict_get(resources, "/XObject")
                xobj_name = _name(operands[0])
                xobj = _dict_get(xobjects, xobj_name) if xobjects else None
                if xobj is None:
                    self.checks.fail(
                        "graphics.xobject_missing",
                        "6.2",
                        f"Content invokes XObject {xobj_name} but it is not defined.",
                        path,
                    )
                elif _name(_dict_get(xobj, "/Subtype")) == "/Form":
                    self._scan_content(
                        xobj,
                        _dict_get(xobj, "/Resources") or resources,
                        f"{path}/XObject{xobj_name}",
                    )
            elif operator == "gs" and operands and resources:
                states = _dict_get(resources, "/ExtGState")
                state_name = _name(operands[0])
                state = _dict_get(states, state_name) if states else None
                if state is not None:
                    self._graphics_state(state, f"{path}/ExtGState{state_name}")

    def _scan_resource_defaults(self, resources: Any, path: str) -> None:
        if not resources:
            return
        spaces = _dict_get(resources, "/ColorSpace")
        if not spaces:
            return
        for key in ("/DefaultRGB", "/DefaultCMYK", "/DefaultGray"):
            value = _dict_get(spaces, key)
            if value is not None:
                self.checks.check(
                    self._is_device_independent_space(value),
                    "color.default_space",
                    "6.2",
                    f"{key} shall resolve to a device-independent color space.",
                    path,
                )

    @staticmethod
    def _has_default_space(resources: Any, key: str) -> bool:
        spaces = _dict_get(resources, "/ColorSpace") if resources else None
        return bool(spaces and _dict_get(spaces, key) is not None)

    @staticmethod
    def _is_device_independent_space(value: Any) -> bool:
        name = _name(value)
        if name in ("/CalRGB", "/CalGray", "/Lab"):
            return True
        if isinstance(value, pikepdf.Array) and value:
            return _name(value[0]) in ("/ICCBased", "/CalRGB", "/CalGray", "/Lab")
        return False

    def _graphics_state(self, state: Any, path: str) -> None:
        try:
            ca = float(_dict_get(state, "/ca", 1) or 1)
            CA = float(_dict_get(state, "/CA", 1) or 1)
        except Exception:
            ca = CA = 0
        smask = _dict_get(state, "/SMask")
        blend = _name(_dict_get(state, "/BM", "/Normal"))
        uses_transparency = (
            ca < 1
            or CA < 1
            or (smask is not None and _name(smask) != "/None")
            or blend not in ("", "/Normal", "/Compatible")
        )
        if self.part == 1:
            self.checks.check(
                not uses_transparency,
                "pdfa1.transparency",
                "6.4",
                "Transparency is prohibited in PDF/A-1.",
                path,
            )
        for key in ("/TR", "/TR2"):
            value = _dict_get(state, key)
            if value is not None:
                self.checks.check(
                    _name(value) == "/Default",
                    "graphics.transfer_function",
                    "6.2",
                    f"{key} transfer functions shall be absent or /Default.",
                    path,
                )

    def _fonts(self) -> None:
        seen: set[tuple[int, int] | str] = set()
        for font, path in self.used_fonts:
            try:
                identity: tuple[int, int] | str = font.objgen
                if identity == (0, 0):
                    identity = path
            except Exception:
                identity = path
            if identity in seen:
                continue
            seen.add(identity)
            subtype = _name(_dict_get(font, "/Subtype"))
            self.checks.check(
                subtype in ("/Type0", "/Type1", "/TrueType", "/Type3", "/MMType1"),
                "fonts.valid_subtype",
                "6.3",
                f"Unsupported or malformed font subtype {subtype!r}.",
                path,
            )
            self.checks.check(
                _is_font_embedded(font),
                "fonts.embedded",
                "6.3",
                "Every font used to render text shall be embedded.",
                path,
            )
            if subtype == "/Type0":
                descendants = _dict_get(font, "/DescendantFonts") or []
                self.checks.check(
                    len(descendants) == 1,
                    "fonts.type0_descendant",
                    "6.3",
                    "A Type0 font shall have exactly one descendant CIDFont.",
                    path,
                )
            if subtype == "/Type3":
                resources = _dict_get(font, "/Resources")
                for glyph_name, charproc in (_dict_get(font, "/CharProcs") or {}).items():
                    self._scan_content(
                        charproc,
                        resources,
                        f"{path}/CharProc{glyph_name}",
                    )

    def _color(self) -> None:
        root = self.pdf.Root
        intents = _dict_get(root, "/OutputIntents") or []
        pdfa_intents: list[Any] = []
        profile_hashes: set[int] = set()
        profile_spaces: set[str] = set()
        for index, intent in enumerate(intents, start=1):
            if _name(_dict_get(intent, "/S")) != "/GTS_PDFA1":
                continue
            pdfa_intents.append(intent)
            path = f"catalog/OutputIntents[{index}]"
            profile = _dict_get(intent, "/DestOutputProfile")
            self.checks.check(
                isinstance(profile, pikepdf.Stream),
                "color.output_intent_profile",
                "6.2",
                "PDF/A OutputIntent requires an embedded DestOutputProfile ICC stream.",
                path,
            )
            if isinstance(profile, pikepdf.Stream):
                try:
                    data = profile.read_bytes()
                    profile_class, color_space, _tags = _icc_info(data)
                    profile_hashes.add(hash(data))
                    profile_spaces.add(color_space.strip())
                    self.checks.check(
                        profile_class in ("mntr", "prtr"),
                        "color.icc_profile_class",
                        "6.2",
                        f"OutputIntent ICC profile class {profile_class!r} is not an output/display class.",
                        path,
                    )
                except Exception as exc:
                    self.checks.fail(
                        "color.icc_valid",
                        "6.2",
                        f"OutputIntent ICC profile is invalid: {exc}",
                        path,
                    )
        self.checks.check(
            bool(pdfa_intents),
            "color.output_intent_required",
            "6.2",
            "A PDF/A OutputIntent with an embedded ICC profile is required.",
            "catalog",
        )
        self.checks.check(
            len(profile_hashes) <= 1,
            "color.output_intent_consistency",
            "6.2",
            "Multiple PDF/A OutputIntents shall not embed conflicting ICC profiles.",
            "catalog",
        )
        self.checks.check(
            not self.used_device_spaces,
            "color.unmanaged_device_space",
            "6.2",
            "Device color operators are used without matching DefaultRGB/DefaultCMYK/DefaultGray replacement spaces: "
            + ", ".join(sorted(self.used_device_spaces)),
            "page content",
        )

    def _embedded_files(self) -> None:
        # Deduplicate filespecs discovered through the complete object graph.
        unique: list[tuple[Any, str]] = []
        seen: set[tuple[int, int] | str] = set()
        for spec, path in self.filespecs:
            try:
                identity: tuple[int, int] | str = spec.objgen
                if identity == (0, 0):
                    identity = path
            except Exception:
                identity = path
            if identity not in seen:
                seen.add(identity)
                unique.append((spec, path))

        if self.part == 1:
            self.checks.check(
                not unique,
                "pdfa1.embedded_files",
                "6.8",
                "Embedded files are prohibited in PDF/A-1.",
                "document",
            )
            return

        for spec, path in unique:
            ef = _dict_get(spec, "/EF")
            if not ef:
                continue
            stream = _dict_get(ef, "/UF") or _dict_get(ef, "/F")
            self.checks.check(
                isinstance(stream, pikepdf.Stream),
                "embedded_file.stream",
                "6.8",
                "Embedded file specification shall reference an embedded file stream.",
                path,
            )
            if not isinstance(stream, pikepdf.Stream):
                continue
            if self.part == 3:
                relationship = _name(_dict_get(spec, "/AFRelationship"))
                self.checks.check(
                    relationship in _ALLOWED_AF_RELATIONSHIPS,
                    "pdfa3.af_relationship",
                    "6.8",
                    "PDF/A-3 embedded files require a valid AFRelationship.",
                    path,
                )
                continue

            # PDF/A-2: attached files must themselves be PDF/A or plain text.
            subtype = _decode_pdf_name(_dict_get(stream, "/Subtype"))
            try:
                data = stream.read_bytes()
            except Exception as exc:
                self.checks.fail(
                    "pdfa2.embedded_file_read",
                    "6.8",
                    f"Embedded file stream could not be decoded: {exc}",
                    path,
                )
                continue
            if subtype.lower() == "text/plain":
                self.checks.passed += 1
                continue
            if subtype.lower() != "application/pdf" and not data.startswith(b"%PDF-"):
                self.checks.fail(
                    "pdfa2.embedded_file_type",
                    "6.8",
                    "PDF/A-2 attachments shall be PDF/A documents or plain text.",
                    path,
                )
                continue
            self._validate_embedded_pdfa(data, path)

    def _validate_embedded_pdfa(self, data: bytes, path: str) -> None:
        if self.recursion_depth >= 4:
            self.checks.fail(
                "pdfa2.embedded_recursion",
                "6.8",
                "Embedded PDF/A nesting exceeds the native validator recursion limit.",
                path,
            )
            return
        try:
            with Pdf.open(BytesIO(data)) as nested:
                claim = {}
                try:
                    with nested.open_metadata(
                        set_pikepdf_as_editor=False,
                        update_docinfo=False,
                        strict=True,
                    ) as meta:
                        claim["part"] = _metadata_value(meta, "pdfaid:part")
                        claim["conformance"] = _metadata_value(meta, "pdfaid:conformance").lower()
                except Exception:
                    pass
                part = claim.get("part")
                conf = claim.get("conformance")
                if part not in ("1", "2", "3") or conf not in ("a", "b", "u"):
                    self.checks.fail(
                        "pdfa2.embedded_pdfa_claim",
                        "6.8",
                        "Embedded PDF does not contain a recognizable PDF/A-1/2/3 identification claim.",
                        path,
                    )
                    return
                nested_flavour = f"{part}b"  # A/U include the Level-B preservation requirements.
                result = _NativeValidationSession(
                    nested,
                    nested_flavour,
                    recursion_depth=self.recursion_depth + 1,
                ).run()
                self.checks.check(
                    result.compliant,
                    "pdfa2.embedded_pdfa_conformance",
                    "6.8",
                    "Embedded PDF does not satisfy the native PDF/A baseline validator: "
                    + ", ".join(result.failed_rules[:8]),
                    path,
                )
        except Exception as exc:
            self.checks.fail(
                "pdfa2.embedded_pdf_parse",
                "6.8",
                f"Embedded PDF could not be parsed: {exc}",
                path,
            )

    def _forms(self) -> None:
        form = _dict_get(self.pdf.Root, "/AcroForm")
        if not form:
            return
        self.checks.check(
            not bool(_dict_get(form, "/NeedAppearances", False)),
            "forms.need_appearances",
            "6.9",
            "AcroForm NeedAppearances shall be false or absent.",
            "catalog/AcroForm",
        )
        self.checks.check(
            _dict_get(form, "/XFA") is None,
            "forms.xfa",
            "6.9",
            "XFA forms are prohibited in PDF/A-1/2/3.",
            "catalog/AcroForm",
        )
        for index, field in enumerate(_iter_fields(_dict_get(form, "/Fields") or []), start=1):
            path = f"catalog/AcroForm/Field[{index}]"
            self._check_action_dictionary(field, path)
            if _name(_dict_get(field, "/Subtype")) == "/Widget":
                self.checks.check(
                    _dict_get(field, "/AP") is not None,
                    "forms.widget_appearance",
                    "6.9",
                    "Widget annotations shall provide an appearance dictionary.",
                    path,
                )


class NativePDFValidator:
    """Validate PDF/A conformance without external validators or subprocesses."""

    def validate(self, path: str | Path, flavour: str) -> ValidationResult:
        get_policy(flavour)  # validate flavour before opening untrusted input
        try:
            with Pdf.open(str(path)) as pdf:
                return _NativeValidationSession(pdf, flavour).run()
        except pikepdf.PasswordError as exc:
            return ValidationResult(
                compliant=False,
                flavour=flavour.lower(),
                failed_checks=1,
                failed_rules=("file.encryption",),
                failures=(
                    ValidationFailure(
                        rule_id="file.encryption",
                        clause="6.1",
                        message="Encrypted PDF cannot conform to PDF/A.",
                        path="trailer",
                    ),
                ),
            )
        except Exception as exc:
            raise ValidationExecutionError(f"Native PDF/A validation failed to parse the document: {exc}") from exc
