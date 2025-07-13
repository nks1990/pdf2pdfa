"""PDF/A-1b conversion logic."""

from __future__ import annotations

import logging
import datetime as dt
import sys
import os
from typing import Optional

try:
    from importlib.resources import files
except ImportError:  # Python <3.9
    from importlib_resources import files
import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array, String

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .fonts import subset_and_embed_fonts
from .icc import embed_icc_profile

logger = logging.getLogger(__name__)


class Converter:
    """Convert arbitrary PDF to PDF/A-1b."""

    def __init__(self, icc_path: Optional[str] = None) -> None:
        if icc_path is not None:
            self.icc_path = icc_path
        else:
            self.icc_path = str(files(__package__).joinpath('data/sRGB.icc.b64'))
        logger.debug("Using ICC profile at %s", self.icc_path)

    def convert(
        self,
        input_path: str,
        output_path: str,
        icc_profile: Optional[str] = None,
        font_path: Optional[str] = None,
    ) -> None:
        """Convert *input_path* to PDF/A-1b and save as *output_path*.

        Parameters
        ----------
        input_path:
            Source PDF file.
        output_path:
            Destination path for the PDF/A-1b file.
        icc_profile:
            Optional path to an ICC profile. If ``None`` the converter's
            default sRGB profile is used.
        font_path:
            Optional path to a TrueType or OpenType font to embed when fonts
            are missing from the source document.
        """

        logger.info("Converting %s -> %s", input_path, output_path)

        # ------------------------------------------------------------------
        # Open the input PDF
        # ------------------------------------------------------------------
        try:
            pdf = Pdf.open(input_path)
        except Exception as exc:
            logger.error("Failed to open PDF %s: %s", input_path, exc)
            raise

        # ------------------------------------------------------------------
        # Determine a font to embed for missing resources
        # ------------------------------------------------------------------
        font_file = font_path
        if font_file is None:
            search: list[str]
            if sys.platform.startswith("win"):
                search = [
                    r"C:\\Windows\\Fonts\\arial.ttf",
                    r"C:\\Windows\\Fonts\\Arial.ttf",
                ]
            elif sys.platform == "darwin":
                search = [
                    "/System/Library/Fonts/Supplemental/Arial.ttf",
                    "/Library/Fonts/Arial.ttf",
                ]
            else:
                search = [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                ]
            for candidate in search:
                if os.path.isfile(candidate):
                    font_file = candidate
                    break

        if font_file and os.path.isfile(font_file):
            logger.debug("Embedding fonts using %s", font_file)
            try:
                # Register with reportlab so fonttools can locate tables
                pdfmetrics.registerFont(TTFont("embed", font_file))
            except Exception:
                pass  # registration failure is non-fatal
            subset_and_embed_fonts(pdf, font_file)
        else:
            logger.warning(
                "No valid font found for embedding; fonts may remain unembedded"
            )

        # ------------------------------------------------------------------
        # Embed ICC profile as OutputIntent
        # ------------------------------------------------------------------
        profile = icc_profile or self.icc_path
        try:
            embed_icc_profile(pdf, profile)
        except FileNotFoundError as exc:
            logger.error("ICC profile not found: %s", profile)
            raise

        # ------------------------------------------------------------------
        # Set up document dates
        # ------------------------------------------------------------------
        now = dt.datetime.utcnow().replace(microsecond=0)
        xmp_date = now.isoformat() + "Z"

        # ------------------------------------------------------------------
        # Existing info dictionary values
        # ------------------------------------------------------------------
        info = pdf.docinfo or Dictionary()
        title = str(info.get(Name.Title, ""))
        author = str(info.get(Name.Author, ""))
        subject = str(info.get(Name.Subject, ""))
        keywords = str(info.get(Name.Keywords, ""))
        producer = str(info.get(Name.Producer, "pdf2pdfa"))

        # ------------------------------------------------------------------
        # Update XMP metadata
        # ------------------------------------------------------------------
        try:
            meta_ctx = pdf.open_metadata(set_pikepdf=True)
        except TypeError:  # pragma: no cover - older pikepdf versions
            meta_ctx = pdf.open_metadata(set_pikepdf_as_editor=True)

        with meta_ctx as md:
            md["pdfaid:part"] = "1"
            md["pdfaid:conformance"] = "B"
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
            md["pdf:Producer"] = producer
            md["xmp:CreateDate"] = xmp_date
            md["xmp:ModifyDate"] = xmp_date

        # ------------------------------------------------------------------
        # Do not update pdf.docinfo directly when using set_pikepdf=True.
        # PikePDF will sync values from XMP on save and warns if we modify
        # docinfo here, because any changes would be overwritten.
        # ------------------------------------------------------------------



        # ------------------------------------------------------------------
        # Save PDF/A
        # ------------------------------------------------------------------
        try:
            try:
                pdf.save(output_path, optimize_version=True)
            except TypeError:
                # Older pikepdf versions do not support optimize_version
                pdf.save(output_path)
        except Exception as exc:
            logger.error("Failed to save PDF %s: %s", output_path, exc)
            raise
        logger.info("Saved PDF/A-1b to %s", output_path)

