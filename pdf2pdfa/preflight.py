"""Conservative PDF preflight for conversion planning.

The preflight deliberately reports features instead of mutating them. The
conversion layer can then choose a safe object-level normalization path or a
full rewrite backend.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pikepdf
from pikepdf import Pdf

from .model import PreflightReport, Severity
from .profiles import get_policy
from .security import open_pdf, validate_input_file


def _name(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def _iter_fields(fields: Iterable[Any]) -> Iterable[Any]:
    for field in fields:
        yield field
        kids = field.get("/Kids")
        if kids:
            yield from _iter_fields(kids)


def _has_javascript_action(obj: Any) -> bool:
    if obj is None:
        return False
    try:
        action = obj.get("/A")
        if action is not None and _name(action.get("/S")) == "/JavaScript":
            return True
        aa = obj.get("/AA")
        if aa:
            for candidate in aa.values():
                if candidate is not None and _name(candidate.get("/S")) == "/JavaScript":
                    return True
    except Exception:
        return False
    return False


def _scan_resources(resources: Any, state: dict[str, Any], seen: set[tuple[int, int]]) -> None:
    if resources is None:
        return
    try:
        objgen = resources.objgen
        if objgen != (0, 0):
            if objgen in seen:
                return
            seen.add(objgen)
    except Exception:
        pass

    fonts = resources.get("/Font")
    if fonts:
        for font in fonts.values():
            subtype = _name(font.get("/Subtype"))
            base = _name(font.get("/BaseFont"))
            descriptor = font.get("/FontDescriptor")
            embedded = bool(
                descriptor
                and any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            )
            if subtype == "/Type0":
                descendants = font.get("/DescendantFonts") or []
                if descendants:
                    descendant = descendants[0]
                    descendant_descriptor = descendant.get("/FontDescriptor")
                    embedded = bool(
                        descendant_descriptor
                        and any(
                            k in descendant_descriptor
                            for k in ("/FontFile", "/FontFile2", "/FontFile3")
                        )
                    )
            state["fonts_total"] += 1
            if not embedded:
                state["fonts_unembedded"] += 1
            if subtype == "/Type0":
                state["type0_fonts"] += 1
            if subtype in ("/Type3", "/CIDFontType0", "/CIDFontType2"):
                state["complex_fonts"] += 1
            state["font_subtypes"][subtype or "unknown"] += 1
            if base:
                state["font_names"].add(base)

    color_spaces = resources.get("/ColorSpace")
    if color_spaces:
        for value in color_spaces.values():
            value_name = _name(value)
            if value_name in ("/DeviceRGB", "/DeviceCMYK", "/DeviceGray"):
                state["device_color_spaces"][value_name] += 1
            elif isinstance(value, pikepdf.Array) and value:
                state["color_space_families"][_name(value[0])] += 1

    ext_gstate = resources.get("/ExtGState")
    if ext_gstate:
        for graphic_state in ext_gstate.values():
            try:
                if float(graphic_state.get("/ca", 1)) < 1 or float(graphic_state.get("/CA", 1)) < 1:
                    state["transparency"] = True
                soft_mask = graphic_state.get("/SMask")
                if soft_mask is not None and _name(soft_mask) != "/None":
                    state["transparency"] = True
            except Exception:
                pass

    xobjects = resources.get("/XObject")
    if xobjects:
        for xobject in xobjects.values():
            subtype = _name(xobject.get("/Subtype"))
            color_space = xobject.get("/ColorSpace")
            color_space_name = _name(color_space)
            if color_space_name in ("/DeviceRGB", "/DeviceCMYK", "/DeviceGray"):
                state["device_color_spaces"][color_space_name] += 1
            if xobject.get("/SMask") is not None:
                state["transparency"] = True
            group = xobject.get("/Group")
            if group and _name(group.get("/S")) == "/Transparency":
                state["transparency"] = True
            if subtype == "/Form":
                _scan_resources(xobject.get("/Resources"), state, seen)


def analyze_pdf(
    path: str | Path,
    level: str = "1b",
    *,
    password: str | bytes | None = None,
    max_input_bytes: int | None = None,
) -> PreflightReport:
    """Inspect *path* and return a profile-aware, non-mutating report.

    Passwords are consumed only by pikepdf in-process. They are never logged or
    handed to a conversion subprocess.
    """
    policy = get_policy(level)
    source = validate_input_file(path, max_bytes=max_input_bytes)
    report = PreflightReport(level=policy.level)
    state: dict[str, Any] = {
        "fonts_total": 0,
        "fonts_unembedded": 0,
        "type0_fonts": 0,
        "complex_fonts": 0,
        "font_subtypes": Counter(),
        "font_names": set(),
        "device_color_spaces": Counter(),
        "color_space_families": Counter(),
        "transparency": False,
    }

    with open_pdf(source, password=password) as pdf:
        root = pdf.Root
        encrypted = bool(getattr(pdf, "is_encrypted", False))
        names = root.get("/Names")
        attachments = bool(names and names.get("/EmbeddedFiles")) or bool(root.get("/AF"))
        javascript = bool(names and names.get("/JavaScript")) or _has_javascript_action(root)

        open_action = root.get("/OpenAction")
        if open_action is not None and _name(open_action.get("/S")) == "/JavaScript":
            javascript = True

        acroform = root.get("/AcroForm")
        signed = False
        if acroform and acroform.get("/Fields"):
            for field in _iter_fields(acroform.get("/Fields")):
                if _name(field.get("/FT")) == "/Sig":
                    signed = True
                    break

        annotations = 0
        seen: set[tuple[int, int]] = set()
        for page in pdf.pages:
            _scan_resources(page.get("/Resources"), state, seen)
            if _has_javascript_action(page):
                javascript = True
            annots = page.get("/Annots") or []
            annotations += len(annots)
            for annot in annots:
                if _has_javascript_action(annot):
                    javascript = True
                appearance = annot.get("/AP")
                if appearance:
                    for value in appearance.values():
                        if hasattr(value, "get"):
                            _scan_resources(value.get("/Resources"), state, seen)

        existing_claim: dict[str, str] = {}
        try:
            with pdf.open_metadata() as metadata:
                if metadata.get("pdfaid:part"):
                    existing_claim["part"] = str(metadata.get("pdfaid:part"))
                if metadata.get("pdfaid:conformance"):
                    existing_claim["conformance"] = str(metadata.get("pdfaid:conformance"))
        except Exception:
            pass

        report.features = {
            "encrypted": encrypted,
            "signed": signed,
            "javascript": javascript,
            "attachments": attachments,
            "transparency": bool(state["transparency"]),
            "annotations": annotations,
            "fonts_total": state["fonts_total"],
            "fonts_unembedded": state["fonts_unembedded"],
            "type0_fonts": state["type0_fonts"],
            "complex_fonts": state["complex_fonts"],
            "font_subtypes": dict(state["font_subtypes"]),
            "font_names": sorted(state["font_names"]),
            "device_color_spaces": dict(state["device_color_spaces"]),
            "color_space_families": dict(state["color_space_families"]),
            "has_output_intent": bool(root.get("/OutputIntents")),
            "existing_pdfa_claim": existing_claim,
            "input_bytes": source.stat().st_size,
        }

    if encrypted and not policy.allow_encryption:
        report.add("encryption", "PDF/A does not permit encryption.", Severity.ERROR, repairable=True)
    if javascript and not policy.allow_javascript:
        report.add("javascript", "JavaScript/actions must be removed for PDF/A.", Severity.ERROR, repairable=True)
    if attachments and not policy.allow_embedded_files:
        report.add(
            "embedded_files",
            f"Embedded files are not permitted by PDF/A-{policy.level}.",
            Severity.ERROR,
            repairable=True,
        )
    if state["transparency"] and not policy.allow_transparency:
        report.add(
            "transparency",
            "Transparency must be flattened for PDF/A-1.",
            Severity.ERROR,
            repairable=True,
        )
    if signed:
        report.add(
            "digital_signature",
            "Conversion changes the byte stream and can invalidate existing signatures.",
            Severity.ERROR,
            repairable=False,
        )
    if state["fonts_unembedded"]:
        report.add(
            "unembedded_fonts",
            f"Found {state['fonts_unembedded']} unembedded font resource(s).",
            Severity.WARNING,
            repairable=True,
            count=state["fonts_unembedded"],
        )
    if state["type0_fonts"]:
        report.add(
            "type0_fonts",
            "Type0/CID fonts require glyph-preserving handling; unsafe dictionary substitution is forbidden.",
            Severity.WARNING,
            repairable=True,
            count=state["type0_fonts"],
        )

    return report
