"""Conservative color-resource normalization for PDF/A conversion."""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path
from typing import Any

from pikepdf import Array, Name, Pdf, Stream

from .icc import make_icc_stream, read_icc_profile

logger = logging.getLogger(__name__)


class ColorProfileMismatchError(ValueError):
    pass


def _stream_components(stream: Stream) -> int:
    try:
        return int(stream.stream_dict.get("/N", 0))
    except Exception:
        return 0


def _bundled_profile(pdf: Pdf, filename: str, expected_components: int) -> Stream:
    path = Path(str(files("pdf2pdfa").joinpath(f"data/{filename}")))
    profile = read_icc_profile(path)
    if profile.components != expected_components:
        raise ColorProfileMismatchError(
            f"Bundled {filename} has N={profile.components}, expected {expected_components}"
        )
    return make_icc_stream(pdf, profile)


def normalize_resource_color_spaces(
    pdf: Pdf,
    *,
    rgb_icc_stream: Stream | None = None,
    cmyk_icc_stream: Stream | None = None,
) -> None:
    """Replace explicit DeviceRGB/DeviceCMYK resource references safely.

    This function intentionally handles resource dictionaries and XObject/
    pattern/shading resources only.  It does *not* pretend to perform a full
    color-managed rewrite of arbitrary content streams; the orchestrator must
    select a rendering/rewrite backend when preflight detects cases outside
    this conservative fast path.
    """
    rgb = rgb_icc_stream or _bundled_profile(pdf, "sRGB.icc.b64", 3)
    cmyk = cmyk_icc_stream or _bundled_profile(pdf, "CMYK.icc.b64", 4)
    if _stream_components(rgb) != 3:
        raise ColorProfileMismatchError("RGB replacement profile must have /N 3")
    if _stream_components(cmyk) != 4:
        raise ColorProfileMismatchError("CMYK replacement profile must have /N 4")

    rgb_cs = Array([Name("/ICCBased"), rgb])
    cmyk_cs = Array([Name("/ICCBased"), cmyk])
    seen: set[tuple[int, int]] = set()

    def replace(value: Any) -> Any:
        if value == Name("/DeviceRGB"):
            return rgb_cs
        if value == Name("/DeviceCMYK"):
            return cmyk_cs
        return value

    def fix_resources(resources: Any) -> None:
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

        color_spaces = resources.get("/ColorSpace")
        if color_spaces:
            for key in list(color_spaces.keys()):
                color_spaces[key] = replace(color_spaces[key])

        xobjects = resources.get("/XObject")
        if xobjects:
            for xobj in xobjects.values():
                if xobj.get("/ColorSpace") is not None:
                    xobj["/ColorSpace"] = replace(xobj.get("/ColorSpace"))
                if xobj.get("/Subtype") == Name("/Form"):
                    fix_resources(xobj.get("/Resources"))

        patterns = resources.get("/Pattern")
        if patterns:
            for pattern in patterns.values():
                if hasattr(pattern, "get"):
                    fix_resources(pattern.get("/Resources"))

        shadings = resources.get("/Shading")
        if shadings:
            for shading in shadings.values():
                if shading.get("/ColorSpace") is not None:
                    shading["/ColorSpace"] = replace(shading.get("/ColorSpace"))

    for page in pdf.pages:
        fix_resources(page.get("/Resources"))
        for annot in page.get("/Annots") or []:
            appearance = annot.get("/AP")
            if not appearance:
                continue
            for candidate in appearance.values():
                if hasattr(candidate, "get"):
                    fix_resources(candidate.get("/Resources"))

    logger.debug("Normalized explicit DeviceRGB/DeviceCMYK resource color spaces")


def sanitize_color_spaces(pdf: Pdf, rgb_icc_stream: Stream) -> None:
    """Backward-compatible wrapper around conservative normalization."""
    normalize_resource_color_spaces(pdf, rgb_icc_stream=rgb_icc_stream)
