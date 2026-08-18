"""Conservative PDF preflight for conversion planning.

The preflight deliberately reports features instead of mutating them.  The
conversion layer can then choose a safe in-place normalization path or a full
rewrite backend.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pikepdf
from pikepdf import Name, Pdf

from .model import PreflightReport, Severity
from .profiles import get_policy


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
                    desc = descendants[0]
                    dfd = desc.get("/FontDescriptor")
                    embedded = bool(
                        dfd and any(k in dfd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
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

    cs = resources.get("/ColorSpace")
    if cs:
        for value in cs.values():
            v = _name(value)
            if v in ("/DeviceRGB", "/DeviceCMYK", "/DeviceGray"):
                state["device_color_spaces"][v] += 1
            elif isinstance(value, pikepdf.Array) and value:
                state["color_space_families"][_name(value[0])] += 1

    ext = resources.get("/ExtGState")
    if ext:
        for gs in ext.values():
            try:
                if float(gs.get("/ca", 1)) < 1 or float(gs.get("/CA", 1)) < 1:
                    state["transparency"] = True
                smask = gs.get("/SMask")
                if smask is not None and _name(smask) != "/None":
                    state["transparency"] = True
            except Exception:
                pass

    xobjects = resources.get("/XObject")
    if xobjects:
        for xobj in xobjects.values():
            subtype = _name(xobj.get("/Subtype"))
            cs_value = xobj.get("/ColorSpace")
            cs_name = _name(cs_value)
            if cs_name in ("/DeviceRGB", "/DeviceCMYK", "/DeviceGray"):
                state["device_color_spaces"][cs_name] += 1
            if xobj.get("/SMask") is not None:
                state["transparency"] = True
            group = xobj.get("/Group")
            if group and _name(group.get("/S")) == "/Transparency":
                state["transparency"] = True
            if subtype == "/Form":
                _scan_resources(xobj.get("/Resources"), state, seen)


def analyze_pdf(path: str | Path, level: str = "1b") -> PreflightReport:
    """Inspect *path* and return a profile-aware, non-mutating report."""
    policy = get_policy(level)
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

    with Pdf.open(str(path)) as pdf:
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
                if _name(field.get("/FT")) == "/Sig" or field.get("/V") is not None and _name(field.get("/FT")) == "/Sig":
                    signed = True
                    break

        annotations = 0
        for page in pdf.pages:
            _scan_resources(page.get("/Resources"), state, set())
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
                            _scan_resources(value.get("/Resources"), state, set())

        existing_claim: dict[str, str] = {}
        try:
            with pdf.open_metadata() as md:
                if md.get("pdfaid:part"):
                    existing_claim["part"] = str(md.get("pdfaid:part"))
                if md.get("pdfaid:conformance"):
                    existing_claim["conformance"] = str(md.get("pdfaid:conformance"))
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
        severity = Severity.WARNING
        report.add(
            "unembedded_fonts",
            f"Found {state['fonts_unembedded']} unembedded font resource(s).",
            severity,
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
