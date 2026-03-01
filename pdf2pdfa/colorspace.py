"""Color space sanitization for PDF/A compliance."""

from __future__ import annotations

import base64
import logging
from importlib.resources import files
from pathlib import Path

import pikepdf
from pikepdf import Pdf, Name, Array, Stream

logger = logging.getLogger(__name__)


def _load_cmyk_profile(pdf: Pdf) -> Stream:
    """Load and embed the bundled CMYK ICC profile."""
    b64_path = files("pdf2pdfa").joinpath("data/CMYK.icc.b64")
    data = base64.b64decode(Path(str(b64_path)).read_text())
    stream = pdf.make_stream(data)
    stream.stream_dict["/N"] = 4
    return stream


def sanitize_color_spaces(pdf: Pdf, rgb_icc_stream: Stream) -> None:
    """Replace device-dependent color spaces with ICC-based equivalents.

    Scans all pages and their XObjects for DeviceRGB and DeviceCMYK
    references and replaces them with ICCBased color spaces using the
    provided sRGB profile and a bundled CMYK profile.
    """
    cmyk_stream = _load_cmyk_profile(pdf)
    rgb_cs = Array([Name("/ICCBased"), rgb_icc_stream])
    cmyk_cs = Array([Name("/ICCBased"), cmyk_stream])

    def _fix_resources(resources):
        """Replace device color spaces in a Resources dictionary."""
        if resources is None:
            return

        # Fix ColorSpace entries
        cs_dict = resources.get("/ColorSpace")
        if cs_dict is not None:
            for key in list(cs_dict.keys()):
                val = cs_dict[key]
                if val == Name("/DeviceRGB"):
                    cs_dict[key] = rgb_cs
                elif val == Name("/DeviceCMYK"):
                    cs_dict[key] = cmyk_cs

        # Recurse into XObjects
        xobjects = resources.get("/XObject")
        if xobjects is not None:
            for key in list(xobjects.keys()):
                xobj = xobjects[key]
                # Image XObjects with /ColorSpace
                cs = xobj.get("/ColorSpace")
                if cs is not None:
                    if cs == Name("/DeviceRGB"):
                        xobj[Name("/ColorSpace")] = rgb_cs
                    elif cs == Name("/DeviceCMYK"):
                        xobj[Name("/ColorSpace")] = cmyk_cs
                # Form XObjects have their own Resources
                subtype = xobj.get("/Subtype")
                if subtype == Name("/Form"):
                    sub_res = xobj.get("/Resources")
                    if sub_res is not None:
                        _fix_resources(sub_res)

    for page in pdf.pages:
        _fix_resources(page.Resources)

    logger.debug("Color spaces sanitized")
