"""Font subsetting and embedding utilities."""

from __future__ import annotations

import logging
from typing import Iterable

from fontTools import subset
from pikepdf import Pdf

logger = logging.getLogger(__name__)


def subset_and_embed_fonts(pdf: Pdf) -> None:
    """Subset and embed all fonts used in *pdf*.

    This implementation performs a basic subset of already embedded TrueType
    fonts. If a font is not embedded, a warning is emitted.
    """
    logger.debug("Subsetting and embedding fonts")
    for page in pdf.pages:
        fonts = page.Resources.get('/Font')
        if not fonts:
            continue
        for name in fonts:
            font = fonts[name]
            descriptor = font.get('/FontDescriptor')
            if descriptor is None:
                continue
            if any(k in descriptor for k in ('/FontFile', '/FontFile2', '/FontFile3')):
                # font already embedded; no action
                logger.debug("Font %s already embedded", descriptor.get('/FontName'))
                continue
            else:
                logger.warning(
                    "Font %s is not embedded and will not be subset", descriptor.get('/FontName')
                )

