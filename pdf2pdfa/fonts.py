"""Font subsetting and embedding utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from fontTools import subset
from fontTools.ttLib import TTFont
from pikepdf import Pdf, Name, Dictionary, Array

logger = logging.getLogger(__name__)


DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _extract_metrics(tt: TTFont) -> dict[str, object]:
    """Return metrics for *tt* scaled to 1000 units."""
    upem = tt['head'].unitsPerEm
    ascent = tt['hhea'].ascent
    descent = tt['hhea'].descent
    bbox = [tt['head'].xMin, tt['head'].yMin, tt['head'].xMax, tt['head'].yMax]
    os2 = tt['OS/2'] if 'OS/2' in tt else None
    cap_height = getattr(os2, 'sCapHeight', ascent)
    italic_angle = tt['post'].italicAngle
    widths = []
    cmap = tt.getBestCmap()
    hmtx = tt['hmtx'].metrics
    for code in range(32, 256):
        gname = cmap.get(code, '.notdef')
        adv = hmtx.get(gname, hmtx.get('.notdef'))[0]
        widths.append(int(round(adv * 1000 / upem)))
    return {
        'bbox': [int(round(v * 1000 / upem)) for v in bbox],
        'ascent': int(round(ascent * 1000 / upem)),
        'descent': int(round(descent * 1000 / upem)),
        'cap_height': int(round(cap_height * 1000 / upem)),
        'italic_angle': italic_angle,
        'widths': widths,
    }


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
    metrics = _extract_metrics(TTFont(str(path)))

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
                    '/FontBBox': Array(metrics['bbox']),
                    '/Ascent': metrics['ascent'],
                    '/Descent': metrics['descent'],
                    '/CapHeight': metrics['cap_height'],
                    '/ItalicAngle': metrics['italic_angle'],
                    '/StemV': 80,
                    '/FontFile2': stream,
                }
            )

            font['/Subtype'] = Name('/TrueType')
            font['/FontDescriptor'] = desc
            font['/FirstChar'] = 32
            font['/LastChar'] = 255
            font['/Widths'] = Array(metrics['widths'])
            font['/Encoding'] = Name('/WinAnsiEncoding')

