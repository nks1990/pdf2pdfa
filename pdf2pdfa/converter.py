"""PDF/A conversion logic (supports PDF/A-1b, 2b, 3b)."""

from __future__ import annotations

import datetime as dt
import logging
from importlib.resources import files
from typing import Optional

import pikepdf
from pikepdf import Dictionary, Name, Pdf

from .colorspace import normalize_resource_color_spaces
from .fonts import subset_and_embed_fonts
from .icc import embed_icc_profile

logger = logging.getLogger(__name__)

_VALID_LEVELS = {"1b", "2b", "3b"}


class Converter:
    """Convert supported PDFs to PDF/A (1b, 2b, or 3b)."""

    def __init__(self, icc_path: Optional[str] = None, level: str = "1b") -> None:
        level = level.lower()
        if level not in _VALID_LEVELS:
            raise ValueError(
                f"Invalid PDF/A level '{level}'. Must be one of: {', '.join(sorted(_VALID_LEVELS))}"
            )
        self.level = level
        self.icc_path = icc_path or str(files(__package__).joinpath("data/sRGB.icc.b64"))
        logger.debug("Using OutputIntent ICC %s (PDF/A-%s)", self.icc_path, self.level)

    def convert(
        self,
        input_path: str,
        output_path: str,
        icc_profile: Optional[str] = None,
        font_path: Optional[str] = None,
    ) -> None:
        part = self.level[0]
        conformance = self.level[1].upper()
        logger.info("Converting %s -> %s (PDF/A-%s)", input_path, output_path, self.level)

        pdf = Pdf.open(input_path)
        try:
            subset_and_embed_fonts(pdf, font_path)

            # OutputIntent describes the intended output condition.  It is not
            # reused blindly as an RGB replacement profile: custom ICC files may
            # legitimately be GRAY or CMYK.
            output_profile = icc_profile or self.icc_path
            embed_icc_profile(pdf, output_profile)

            # Explicit device color resources get dedicated, bundled profiles
            # with matching component counts (RGB N=3, CMYK N=4).
            normalize_resource_color_spaces(pdf)

            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            xmp_date = now.isoformat()
            info = pdf.docinfo or Dictionary()
            title = str(info.get(Name.Title, ""))
            author = str(info.get(Name.Author, ""))
            subject = str(info.get(Name.Subject, ""))
            keywords = str(info.get(Name.Keywords, ""))

            with pdf.open_metadata(set_pikepdf_as_editor=True) as md:
                md["pdfaid:part"] = part
                md["pdfaid:conformance"] = conformance
                md["dc:format"] = "application/pdf"
                if title:
                    md["dc:title"] = title
                if author:
                    md["dc:creator"] = [author]
                if subject:
                    md["dc:description"] = subject
                if keywords:
                    md["pdf:Keywords"] = keywords
                md["xmp:CreatorTool"] = "pdf2pdfa"
                md["xmp:CreateDate"] = xmp_date
                md["xmp:ModifyDate"] = xmp_date

            with pdf.open_metadata(set_pikepdf_as_editor=False) as md:
                md["pdf:Producer"] = f"pikepdf {pikepdf.__version__} (pdf2pdfa)"

            try:
                pdf.save(output_path, optimize_version=True)
            except TypeError:
                pdf.save(output_path)
        finally:
            pdf.close()

        logger.info("Saved PDF/A-%s to %s", self.level, output_path)
