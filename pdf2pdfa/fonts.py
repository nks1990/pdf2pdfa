"""Font subsetting and embedding utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from fontTools.ttLib import TTFont
from pikepdf import Pdf, Name, Dictionary, Array

from .font_resolver import resolve_font

logger = logging.getLogger(__name__)

# WinAnsiEncoding codes 128-159 map to these Unicode codepoints.
# Codes 32-127 and 160-255 are identical to Unicode.
_WIN_ANSI_TO_UNICODE: dict[int, int] = {
    128: 0x20AC, 129: 0x2022, 130: 0x201A, 131: 0x0192,
    132: 0x201E, 133: 0x2026, 134: 0x2020, 135: 0x2021,
    136: 0x02C6, 137: 0x2030, 138: 0x0160, 139: 0x2039,
    140: 0x0152, 141: 0x2022, 142: 0x017D, 143: 0x2022,
    144: 0x2022, 145: 0x2018, 146: 0x2019, 147: 0x201C,
    148: 0x201D, 149: 0x2022, 150: 0x2013, 151: 0x2014,
    152: 0x02DC, 153: 0x2122, 154: 0x0161, 155: 0x203A,
    156: 0x0153, 157: 0x2022, 158: 0x017E, 159: 0x0178,
}


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
        uni = _WIN_ANSI_TO_UNICODE.get(code, code)
        gname = cmap.get(uni, '.notdef')
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


def _load_font(font_path: str, cache: dict[str, tuple[bytes, dict]]) -> tuple[bytes, dict] | None:
    """Load font data and metrics, using *cache* to avoid re-reading."""
    if font_path in cache:
        return cache[font_path]
    path = Path(font_path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    metrics = _extract_metrics(TTFont(str(path)))
    cache[font_path] = (data, metrics)
    return (data, metrics)


def subset_and_embed_fonts(pdf: Pdf, font_path: str | None = None) -> None:
    """Embed all fonts used in *pdf*.

    Each unembedded font is resolved individually via :func:`resolve_font`,
    matching the PDF font name to the best system substitute by family,
    weight, and style.  If *font_path* is given it acts as a user override
    (every font gets that file).
    """
    cache: dict[str, tuple[bytes, dict]] = {}

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

            base_font = str(font.get('/BaseFont', '/Unknown'))
            resolved = resolve_font(base_font, font_path)
            if resolved is None:
                logger.warning("No font found for %s; skipping", base_font)
                continue

            logger.debug("Embedding %s → %s", base_font, resolved)
            loaded = _load_font(resolved, cache)
            if loaded is None:
                logger.warning("Could not load %s; skipping %s", resolved, base_font)
                continue

            font_data, metrics = loaded
            stream = pdf.make_stream(font_data)
            desc = Dictionary(
                {
                    '/Type': Name('/FontDescriptor'),
                    '/FontName': font.get('/BaseFont', Name('/Unknown')),
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

