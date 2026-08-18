"""Conservative font embedding utilities.

The module refuses transformations that cannot preserve the original PDF
character-code -> glyph mapping. Complex fonts must be handled by a full
rewrite backend instead of rewriting their dictionaries in place.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fontTools.ttLib import TTFont
from pikepdf import Array, Dictionary, Name, Pdf

from .font_resolver import parse_font_name, resolve_font

logger = logging.getLogger(__name__)


class UnsafeFontSubstitutionError(RuntimeError):
    """Raised when in-place embedding could alter text or glyph mapping."""


_WIN_ANSI_TO_UNICODE: dict[int, int] = {
    128: 0x20AC,
    129: 0x2022,
    130: 0x201A,
    131: 0x0192,
    132: 0x201E,
    133: 0x2026,
    134: 0x2020,
    135: 0x2021,
    136: 0x02C6,
    137: 0x2030,
    138: 0x0160,
    139: 0x2039,
    140: 0x0152,
    141: 0x2022,
    142: 0x017D,
    143: 0x2022,
    144: 0x2022,
    145: 0x2018,
    146: 0x2019,
    147: 0x201C,
    148: 0x201D,
    149: 0x2022,
    150: 0x2013,
    151: 0x2014,
    152: 0x02DC,
    153: 0x2122,
    154: 0x0161,
    155: 0x203A,
    156: 0x0153,
    157: 0x2022,
    158: 0x017E,
    159: 0x0178,
}

_STANDARD14_SAFE = {
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
}
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_PS_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.+-]")


def _embedded_descriptor(font) -> object | None:
    subtype = str(font.get("/Subtype", ""))
    if subtype == "/Type0":
        descendants = font.get("/DescendantFonts") or []
        if descendants:
            return descendants[0].get("/FontDescriptor")
        return None
    return font.get("/FontDescriptor")


def _is_embedded(font) -> bool:
    descriptor = _embedded_descriptor(font)
    return bool(
        descriptor
        and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
    )


def _postscript_name(tt: TTFont) -> str:
    name_table = tt["name"]
    preferred = name_table.getName(6, 3, 1, 0x409)
    candidates = [preferred] if preferred is not None else []
    candidates.extend(record for record in name_table.names if record.nameID == 6)
    for record in candidates:
        if record is None:
            continue
        try:
            raw = record.toUnicode().strip()
        except Exception:
            continue
        sanitized = _PS_NAME_SAFE_RE.sub("", raw.replace(" ", ""))
        if sanitized:
            return sanitized
    raise UnsafeFontSubstitutionError("Embedded TrueType font has no usable PostScript name")


def _extract_metrics(tt: TTFont) -> dict[str, object]:
    upem = tt["head"].unitsPerEm
    ascent = tt["hhea"].ascent
    descent = tt["hhea"].descent
    bbox = [tt["head"].xMin, tt["head"].yMin, tt["head"].xMax, tt["head"].yMax]
    os2 = tt["OS/2"] if "OS/2" in tt else None
    cap_height = getattr(os2, "sCapHeight", ascent)
    post = tt["post"]
    italic_angle = post.italicAngle
    cmap = tt.getBestCmap() or {}
    hmtx = tt["hmtx"].metrics
    notdef_width = hmtx.get(".notdef", (upem, 0))[0]
    widths: list[int] = []
    for code in range(32, 256):
        uni = _WIN_ANSI_TO_UNICODE.get(code, code)
        glyph = cmap.get(uni, ".notdef")
        advance = hmtx.get(glyph, (notdef_width, 0))[0]
        widths.append(int(round(advance * 1000 / upem)))
    return {
        "bbox": [int(round(v * 1000 / upem)) for v in bbox],
        "ascent": int(round(ascent * 1000 / upem)),
        "descent": int(round(descent * 1000 / upem)),
        "cap_height": int(round(cap_height * 1000 / upem)),
        "italic_angle": italic_angle,
        "is_fixed_pitch": bool(getattr(post, "isFixedPitch", 0)),
        "postscript_name": _postscript_name(tt),
        "widths": widths,
    }


def _load_font(
    font_path: str,
    cache: dict[str, tuple[bytes, dict[str, object]]],
) -> tuple[bytes, dict[str, object]] | None:
    if font_path in cache:
        return cache[font_path]
    path = Path(font_path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    with TTFont(str(path), lazy=False) as tt:
        metrics = _extract_metrics(tt)
    cache[font_path] = (data, metrics)
    return data, metrics


def _safe_simple_font(font, base_font: str, explicit_override: bool) -> None:
    subtype = str(font.get("/Subtype", ""))
    if subtype == "/Type3":
        return
    if subtype == "/Type0" or font.get("/DescendantFonts") is not None:
        raise UnsafeFontSubstitutionError(
            f"{base_font}: Type0/CID font requires a glyph-preserving rewrite backend"
        )
    if subtype not in ("/Type1", "/TrueType", ""):
        raise UnsafeFontSubstitutionError(
            f"{base_font}: unsupported font subtype {subtype or '<missing>'}"
        )
    encoding = str(font.get("/Encoding", ""))
    if encoding != "/WinAnsiEncoding":
        raise UnsafeFontSubstitutionError(
            f"{base_font}: only explicit WinAnsiEncoding is safe for in-place embedding"
        )
    plain = _SUBSET_RE.sub("", base_font.lstrip("/"))
    if plain in ("Symbol", "ZapfDingbats"):
        raise UnsafeFontSubstitutionError(
            f"{base_font}: symbolic encodings must not be replaced with a Latin font"
        )
    if not explicit_override and plain not in _STANDARD14_SAFE:
        raise UnsafeFontSubstitutionError(
            f"{base_font}: arbitrary font substitution is disabled; use a rewrite backend"
        )


def _font_flags(base_font: str, metrics: dict[str, object]) -> int:
    category, _, style = parse_font_name(base_font)
    flags = 32  # Nonsymbolic
    if category == "mono" or bool(metrics["is_fixed_pitch"]):
        flags |= 1
    if category == "serif":
        flags |= 2
    if style == "italic" or float(metrics["italic_angle"]) != 0:
        flags |= 64
    return flags


def embed_missing_fonts(pdf: Pdf, font_path: str | None = None) -> None:
    """Embed only simple fonts whose character-code mapping can be preserved.

    The fast path supports explicit WinAnsi-encoded simple fonts. Standard 14
    fonts may require a platform substitute because their programs are normally
    not embedded in the source PDF. In that case the dictionary is updated to
    the *actual* embedded font program name and widths; the original encoding is
    preserved. This preserves code-to-Unicode semantics but can still produce
    small visual/metric differences, which is why strict fidelity-sensitive
    workflows should validate/render-compare their corpus.
    """
    cache: dict[str, tuple[bytes, dict[str, object]]] = {}
    seen: set[tuple[int, int] | str] = set()

    for page in pdf.pages:
        fonts = page.Resources.get("/Font")
        if not fonts:
            continue
        for resource_name in list(fonts.keys()):
            font = fonts[resource_name]
            if _is_embedded(font):
                continue
            if str(font.get("/Subtype", "")) == "/Type3":
                # Type3 glyph programs live in the PDF itself; do not rewrite them.
                continue

            base_font = str(font.get("/BaseFont", "/Unknown"))
            try:
                identity: tuple[int, int] | str = font.objgen
                if identity == (0, 0):
                    identity = f"{resource_name}:{base_font}"
            except Exception:
                identity = f"{resource_name}:{base_font}"
            if identity in seen:
                continue
            seen.add(identity)

            _safe_simple_font(font, base_font, explicit_override=font_path is not None)
            resolved = resolve_font(base_font, font_path)
            if resolved is None:
                raise UnsafeFontSubstitutionError(
                    f"No embeddable font program found for {base_font}"
                )
            loaded = _load_font(resolved, cache)
            if loaded is None:
                raise UnsafeFontSubstitutionError(f"Could not load font program {resolved}")

            font_data, metrics = loaded
            embedded_name = Name(f"/{metrics['postscript_name']}")
            stream = pdf.make_stream(font_data)
            descriptor = Dictionary(
                {
                    "/Type": Name("/FontDescriptor"),
                    "/FontName": embedded_name,
                    "/Flags": _font_flags(base_font, metrics),
                    "/FontBBox": Array(metrics["bbox"]),
                    "/Ascent": metrics["ascent"],
                    "/Descent": metrics["descent"],
                    "/CapHeight": metrics["cap_height"],
                    "/ItalicAngle": metrics["italic_angle"],
                    "/StemV": 80,
                    "/FontFile2": stream,
                }
            )
            font["/Subtype"] = Name("/TrueType")
            font["/BaseFont"] = embedded_name
            font["/FontDescriptor"] = descriptor
            font["/FirstChar"] = 32
            font["/LastChar"] = 255
            font["/Widths"] = Array(metrics["widths"])
            # Encoding is intentionally preserved, never overwritten.
            logger.debug(
                "Embedded %s using %s as %s",
                base_font,
                resolved,
                metrics["postscript_name"],
            )


def subset_and_embed_fonts(pdf: Pdf, font_path: str | None = None) -> None:
    """Backward-compatible alias for :func:`embed_missing_fonts`."""
    embed_missing_fonts(pdf, font_path)
