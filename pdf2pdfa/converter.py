"""PDF/A-1b conversion logic."""

from __future__ import annotations

import logging
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

    def convert(self, input_path: str, output_path: str) -> None:
        """Convert *input_path* to PDF/A-1b and save as *output_path*."""
        logger.info("Converting %s -> %s", input_path, output_path)
        try:
            pdf = Pdf.open(input_path)
        except Exception as exc:
            logger.error("Failed to open PDF %s: %s", input_path, exc)
            raise

        subset_and_embed_fonts(pdf)
        embed_icc_profile(pdf, self.icc_path)
        xmp = generate_xmp_metadata()
        pdf.Root.Metadata = pdf.make_stream(xmp.encode("utf-8"))

        try:
            pdf.save(output_path)
        except Exception as exc:
            logger.error("Failed to save PDF %s: %s", output_path, exc)
            raise
        logger.info("Saved PDF/A-1b to %s", output_path)

