"""PDF/A conversion logic (supports PDF/A-1b, 2b, 3b)."""

from __future__ import annotations

import datetime as dt
import logging
from importlib.resources import files
from typing import Optional

import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array, String

from .colorspace import sanitize_color_spaces
from .fonts import subset_and_embed_fonts
from .icc import embed_icc_profile

logger = logging.getLogger(__name__)


_VALID_LEVELS = {"1b", "2b", "3b"}


class Converter:
    """Convert arbitrary PDF to PDF/A (1b, 2b, or 3b)."""

    def __init__(
        self,
        icc_path: Optional[str] = None,
        level: str = "1b",
    ) -> None:
        level = level.lower()
        if level not in _VALID_LEVELS:
            raise ValueError(
                f"Invalid PDF/A level '{level}'. Must be one of: {', '.join(sorted(_VALID_LEVELS))}"
            )
        self.level = level
        if icc_path is not None:
            self.icc_path = icc_path
        else:
            self.icc_path = str(files(__package__).joinpath('data/sRGB.icc.b64'))
        logger.debug("Using ICC profile at %s (level=PDF/A-%s)", self.icc_path, self.level)

    def convert(
        self,
        input_path: str,
        output_path: str,
        icc_profile: Optional[str] = None,
        font_path: Optional[str] = None,
    ) -> None:
        """Convert *input_path* to PDF/A and save as *output_path*."""

        part = self.level[0]        # "1", "2", or "3"
        conformance = self.level[1].upper()  # "B"

        logger.info("Converting %s -> %s (PDF/A-%s)", input_path, output_path, self.level)

        try:
            pdf = Pdf.open(input_path)
        except Exception as exc:
            logger.error("Failed to open PDF %s: %s", input_path, exc)
            raise

        # ------------------------------------------------------------------
        # Embed fonts (resolver picks the best match per font)
        # ------------------------------------------------------------------
        subset_and_embed_fonts(pdf, font_path)

        # ------------------------------------------------------------------
        # Embed ICC profile as OutputIntent
        # ------------------------------------------------------------------
        profile = icc_profile or self.icc_path
        try:
            icc_stream = embed_icc_profile(pdf, profile)
        except FileNotFoundError:
            logger.error("ICC profile not found: %s", profile)
            raise

        # ------------------------------------------------------------------
        # Sanitize color spaces (DeviceRGB/DeviceCMYK -> ICCBased)
        # ------------------------------------------------------------------
        sanitize_color_spaces(pdf, icc_stream)

        # ------------------------------------------------------------------
        # Set up document dates
        # ------------------------------------------------------------------
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        xmp_date = now.isoformat()

        # ------------------------------------------------------------------
        # Existing info dictionary values
        # ------------------------------------------------------------------
        info = pdf.docinfo or Dictionary()
        title = str(info.get(Name.Title, ""))
        author = str(info.get(Name.Author, ""))
        subject = str(info.get(Name.Subject, ""))
        keywords = str(info.get(Name.Keywords, ""))

        # ------------------------------------------------------------------
        # Update XMP metadata
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Save PDF/A
        # ------------------------------------------------------------------
        try:
            try:
                pdf.save(output_path, optimize_version=True)
            except TypeError:
                pdf.save(output_path)
        except Exception as exc:
            logger.error("Failed to save PDF %s: %s", output_path, exc)
            raise
        logger.info("Saved PDF/A-%s to %s", self.level, output_path)

        # ------------------------------------------------------------------
        # Verify XMP / docinfo sync (non-fatal)
        # ------------------------------------------------------------------
        try:
            with Pdf.open(output_path) as out_pdf:
                with out_pdf.open_metadata() as md:
                    out_info = out_pdf.docinfo
                    title_md = md.get("dc:title")
                    if str(out_info.get(Name.Title, "")) != str(title_md or ""):
                        logger.warning("XMP/docinfo title mismatch")
        except Exception:
            logger.warning("Could not verify metadata sync")
