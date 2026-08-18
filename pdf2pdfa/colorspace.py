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


class FullColorRewriteRequired(RuntimeError):
    """Raised when assigning an arbitrary profile would be semantically unsafe."""


def _stream_components(stream: Stream) -> int:
    try:
        return int(stream.stream_dict.get("/N", 0))
    except Exception:
        return 0


def _bundled_rgb_profile(pdf: Pdf) -> Stream:
    path = Path(str(files("pdf2pdfa").joinpath("data/sRGB.icc.b64")))
    profile = read_icc_profile(path)
    if profile.components != 3:
        raise ColorProfileMismatchError(
            f"Bundled sRGB profile has N={profile.components}, expected 3"
        )
    return make_icc_stream(pdf, profile)


def normalize_resource_color_spaces(
    pdf: Pdf,
    *,
    rgb_icc_stream: Stream | None = None,
    cmyk_icc_stream: Stream | None = None,
) -> None:
    """Normalize explicit device color *resources* without guessing CMYK intent.

    DeviceRGB resources may be calibrated to the bundled sRGB profile. A
    DeviceCMYK resource is only rewritten if the caller supplies an explicit
    four-component CMYK ICC stream whose source condition it intentionally
    wants to assign. The normal converter does not guess one: automatic mode
    routes CMYK documents to the full color-managed rewrite backend instead.

    Content-stream device-color operators are outside this function's scope and
    are detected by preflight so they can also route to a full rewrite.
    """
    rgb = rgb_icc_stream or _bundled_rgb_profile(pdf)
    if _stream_components(rgb) != 3:
        raise ColorProfileMismatchError("RGB replacement profile must have /N 3")
    if cmyk_icc_stream is not None and _stream_components(cmyk_icc_stream) != 4:
        raise ColorProfileMismatchError("CMYK replacement profile must have /N 4")

    rgb_cs = Array([Name("/ICCBased"), rgb])
    cmyk_cs = (
        Array([Name("/ICCBased"), cmyk_icc_stream])
        if cmyk_icc_stream is not None
        else None
    )
    seen: set[tuple[int, int]] = set()

    def replace(value: Any) -> Any:
        if value == Name("/DeviceRGB"):
            return rgb_cs
        if value == Name("/DeviceCMYK"):
            if cmyk_cs is None:
                raise FullColorRewriteRequired(
                    "DeviceCMYK requires a full color-managed rewrite unless an explicit "
                    "source CMYK ICC profile is supplied"
                )
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

    logger.debug("Normalized explicit device-color resource references")


def sanitize_color_spaces(pdf: Pdf, rgb_icc_stream: Stream) -> None:
    """Backward-compatible wrapper around conservative RGB normalization."""
    normalize_resource_color_spaces(pdf, rgb_icc_stream=rgb_icc_stream)
