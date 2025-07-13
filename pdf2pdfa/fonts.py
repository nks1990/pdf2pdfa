"""Font subsetting and embedding utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from fontTools import subset
from pikepdf import Pdf, Name, Dictionary, Array

logger = logging.getLogger(__name__)


DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def subset_and_embed_fonts(pdf: Pdf, font_path: str = DEFAULT_FONT_PATH) -> None:
    """Embed all fonts used in *pdf*.

    Fonts already embedded are left untouched. For fonts that are missing, a
    generic TrueType font is embedded so that the resulting document contains
    embedded font programs for all resources. This implementation is intentionally
    simple and primarily intended for test documents.
    """

    logger.debug("Embedding fonts using %s", font_path)

    path = Path(font_path)
    if not path.is_file():
        logger.warning("Font file %s not found; fonts may remain unembedded", font_path)
        return

    font_data = path.read_bytes()

    for page in pdf.pages:
        fonts = page.Resources.get('/Font')
        if not fonts:
            continue
        for name in list(fonts.keys()):
            font = fonts[name]
            descriptor = font.get('/FontDescriptor')
            if descriptor and any(k in descriptor for k in ('/FontFile', '/FontFile2', '/FontFile3')):
                logger.debug("Font %s already embedded", descriptor.get('/FontName'))
                continue

            logger.debug("Embedding missing font %s", font.get('/BaseFont'))
            stream = pdf.make_stream(font_data)
            desc = Dictionary(
                {
                    '/Type': Name('/FontDescriptor'),
                    '/FontName': font.get('/BaseFont', Name('/DejaVuSans')),
                    '/Flags': 32,
                    '/FontBBox': Array([0, -200, 1000, 900]),
                    '/Ascent': 800,
                    '/Descent': -200,
                    '/CapHeight': 700,
                    '/ItalicAngle': 0,
                    '/StemV': 80,
                    '/FontFile2': stream,
                }
            )

            font['/Subtype'] = Name('/TrueType')
            font['/FontDescriptor'] = desc
            font['/FirstChar'] = 32
            font['/LastChar'] = 255
            font['/Widths'] = Array([600] * 224)
            font['/Encoding'] = Name('/WinAnsiEncoding')

