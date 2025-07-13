"""PDF/A-1b conversion logic."""

from __future__ import annotations

import logging
import datetime as dt
from typing import Optional

try:
    from importlib.resources import files
except ImportError:  # Python <3.9
    from importlib_resources import files
import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array, String

from .fonts import subset_and_embed_fonts
from .icc import embed_icc_profile
from .metadata import generate_xmp_metadata

logger = logging.getLogger(__name__)


class Converter:
    """Convert arbitrary PDF to PDF/A-1b."""

    def __init__(self, icc_path: Optional[str] = None) -> None:
        if icc_path is not None:
            self.icc_path = icc_path
        else:
            self.icc_path = str(files(__package__).joinpath('data/sRGB.icc.b64'))
        logger.debug("Using ICC profile at %s", self.icc_path)

    def convert(self, input_path: str, output_path: str, icc_profile: Optional[str] = None) -> None:
        """Convert *input_path* to PDF/A-1b and save as *output_path*."""

        logger.info("Converting %s -> %s", input_path, output_path)
        try:
            pdf = Pdf.open(input_path)
        except Exception as exc:
            logger.error("Failed to open PDF %s: %s", input_path, exc)
            raise

        # Embed fonts to satisfy PDF/A requirements
        subset_and_embed_fonts(pdf)

        # Embed the ICC profile as an OutputIntent
        profile = icc_profile or self.icc_path
        embed_icc_profile(pdf, profile)

        now = dt.datetime.utcnow()
        pdf_date = now.strftime("D:%Y%m%d%H%M%S+00'00'")

        # Populate document information dictionary
        info = pdf.docinfo or Dictionary()
        info[Name.Title] = info.get(Name.Title, String(""))
        info[Name.Author] = info.get(Name.Author, String(""))
        info[Name.Subject] = info.get(Name.Subject, String(""))
        info[Name.Keywords] = info.get(Name.Keywords, String(""))
        info[Name.Producer] = info.get(Name.Producer, String("pdf2pdfa"))
        info[Name.CreationDate] = pdf_date
        info[Name.ModDate] = pdf_date
        info[Name.Creator] = info.get(Name.Creator, String("pdf2pdfa"))
        pdf.docinfo = info

        # Synchronize XMP metadata
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as meta:
            meta["pdfaid:part"] = "1"
            meta["pdfaid:conformance"] = "B"
            meta["dc:title"] = str(info.get(Name.Title, ""))
            meta["dc:creator"] = str(info.get(Name.Author, ""))
            if info.get(Name.Subject):
                meta["dc:description"] = str(info.get(Name.Subject))
            if info.get(Name.Keywords):
                meta["pdf:Keywords"] = str(info.get(Name.Keywords))
            meta["pdf:Producer"] = str(info.get(Name.Producer, "pdf2pdfa"))
            meta["xmp:CreatorTool"] = str(info.get(Name.Creator, "pdf2pdfa"))
            meta["xmp:CreateDate"] = pdf_date
            meta["xmp:ModifyDate"] = pdf_date

        try:
            pdf.save(output_path)
        except Exception as exc:
            logger.error("Failed to save PDF %s: %s", output_path, exc)
            raise
        logger.info("Saved PDF/A-1b to %s", output_path)

