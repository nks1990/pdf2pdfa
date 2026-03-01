"""Font resolver — match PDF font names to system TrueType files."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ── PDF Standard 14 fonts ─────────────────────────────────────────────

_PDF_STANDARD_14: dict[str, tuple[str, str, str]] = {
    # Helvetica family
    "Helvetica":             ("sans", "normal", "roman"),
    "Helvetica-Bold":        ("sans", "bold",   "roman"),
    "Helvetica-Oblique":     ("sans", "normal", "italic"),
    "Helvetica-BoldOblique": ("sans", "bold",   "italic"),
    # Times family
    "Times-Roman":           ("serif", "normal", "roman"),
    "Times-Bold":            ("serif", "bold",   "roman"),
    "Times-Italic":          ("serif", "normal", "italic"),
    "Times-BoldItalic":      ("serif", "bold",   "italic"),
    # Courier family
    "Courier":               ("mono", "normal", "roman"),
    "Courier-Bold":          ("mono", "bold",   "roman"),
    "Courier-Oblique":       ("mono", "normal", "italic"),
    "Courier-BoldOblique":   ("mono", "bold",   "italic"),
    # Symbol / ZapfDingbats — treat as sans fallback
    "Symbol":                ("sans", "normal", "roman"),
    "ZapfDingbats":          ("sans", "normal", "roman"),
}

# ── Family aliases → category ─────────────────────────────────────────

_FAMILY_ALIASES: dict[str, str] = {
    # sans-serif
    "arial":          "sans",
    "helvetica":      "sans",
    "liberationsans": "sans",
    "dejavu sans":    "sans",
    "dejavusans":     "sans",
    "calibri":        "sans",
    "verdana":        "sans",
    "tahoma":         "sans",
    "segoeui":        "sans",
    "roboto":         "sans",
    "opensans":       "sans",
    "noto sans":      "sans",
    "notosans":       "sans",
    # serif
    "times":          "serif",
    "timesnewroman":  "serif",
    "times new roman":"serif",
    "georgia":        "serif",
    "garamond":       "serif",
    "palatino":       "serif",
    "bookantiqua":    "serif",
    "cambria":        "serif",
    "liberationserif":"serif",
    "noto serif":     "serif",
    "notoserif":      "serif",
    # monospace
    "courier":        "mono",
    "couriernew":     "mono",
    "courier new":    "mono",
    "consolas":       "mono",
    "lucidaconsole":  "mono",
    "liberationmono": "mono",
    "dejavu sans mono":"mono",
    "dejavusansmono": "mono",
    "noto mono":      "mono",
    "notomono":       "mono",
}

# ── System font paths per platform ────────────────────────────────────
# Keys: (category, weight, style) → file path

_SYSTEM_FONTS: dict[str, dict[tuple[str, str, str], str]] = {
    "win32": {
        # sans
        ("sans", "normal", "roman"):  r"C:\Windows\Fonts\arial.ttf",
        ("sans", "bold",   "roman"):  r"C:\Windows\Fonts\arialbd.ttf",
        ("sans", "normal", "italic"): r"C:\Windows\Fonts\ariali.ttf",
        ("sans", "bold",   "italic"): r"C:\Windows\Fonts\arialbi.ttf",
        # serif
        ("serif", "normal", "roman"):  r"C:\Windows\Fonts\times.ttf",
        ("serif", "bold",   "roman"):  r"C:\Windows\Fonts\timesbd.ttf",
        ("serif", "normal", "italic"): r"C:\Windows\Fonts\timesi.ttf",
        ("serif", "bold",   "italic"): r"C:\Windows\Fonts\timesbi.ttf",
        # mono
        ("mono", "normal", "roman"):  r"C:\Windows\Fonts\cour.ttf",
        ("mono", "bold",   "roman"):  r"C:\Windows\Fonts\courbd.ttf",
        ("mono", "normal", "italic"): r"C:\Windows\Fonts\couri.ttf",
        ("mono", "bold",   "italic"): r"C:\Windows\Fonts\courbi.ttf",
    },
    "darwin": {
        # sans
        ("sans", "normal", "roman"):  "/System/Library/Fonts/Supplemental/Arial.ttf",
        ("sans", "bold",   "roman"):  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ("sans", "normal", "italic"): "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
        ("sans", "bold",   "italic"): "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
        # serif
        ("serif", "normal", "roman"):  "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        ("serif", "bold",   "roman"):  "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        ("serif", "normal", "italic"): "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
        ("serif", "bold",   "italic"): "/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf",
        # mono
        ("mono", "normal", "roman"):  "/System/Library/Fonts/Supplemental/Courier New.ttf",
        ("mono", "bold",   "roman"):  "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        ("mono", "normal", "italic"): "/System/Library/Fonts/Supplemental/Courier New Italic.ttf",
        ("mono", "bold",   "italic"): "/System/Library/Fonts/Supplemental/Courier New Bold Italic.ttf",
    },
    "linux": {
        # sans
        ("sans", "normal", "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ("sans", "bold",   "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ("sans", "normal", "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        ("sans", "bold",   "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        # serif
        ("serif", "normal", "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ("serif", "bold",   "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ("serif", "normal", "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        ("serif", "bold",   "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf",
        # mono
        ("mono", "normal", "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ("mono", "bold",   "roman"):  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        ("mono", "normal", "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf",
        ("mono", "bold",   "italic"): "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-BoldOblique.ttf",
    },
}

# ── Regex for subset prefix (e.g. ABCDEF+) ───────────────────────────

_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")


def _current_platform() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def parse_font_name(base_font: str) -> tuple[str, str, str]:
    """Parse a PDF font name into ``(category, weight, style)``.

    *base_font* is the raw ``/BaseFont`` value from the PDF
    (e.g. ``/ABCDEF+Helvetica-BoldOblique``).

    Returns one of:
    - category: ``"sans"`` | ``"serif"`` | ``"mono"``
    - weight:   ``"normal"`` | ``"bold"``
    - style:    ``"roman"`` | ``"italic"``
    """
    # Strip leading '/'
    name = base_font.lstrip("/")

    # Strip subset prefix (ABCDEF+)
    name = _SUBSET_RE.sub("", name)

    # Exact match in standard-14
    if name in _PDF_STANDARD_14:
        return _PDF_STANDARD_14[name]

    # Heuristic parse
    upper = name.upper()
    weight = "bold" if "BOLD" in upper else "normal"
    style = "italic" if ("ITALIC" in upper or "OBLIQUE" in upper) else "roman"

    # Strip weight/style tokens to isolate family
    family = re.sub(r"[-,]?(Bold|Italic|Oblique|Regular|Medium|Light|Semi|Demi|Extra|Condensed|MT|PS)", "", name, flags=re.IGNORECASE)
    family = family.strip("-").strip()
    family_lower = family.lower().replace(" ", "").replace("-", "")

    # Lookup family alias
    category = _FAMILY_ALIASES.get(family_lower, None)
    if category is None:
        # Try with spaces preserved
        category = _FAMILY_ALIASES.get(family.lower(), None)
    if category is None:
        category = "sans"

    return (category, weight, style)


def resolve_font(base_font: str, user_font_path: str | None = None) -> str | None:
    """Resolve a PDF font name to a system TrueType file path.

    If *user_font_path* is given and exists, it is returned immediately
    (user override takes precedence).

    Otherwise, the font name is parsed and matched against system fonts
    for the current platform. Falls back through degradation:
    bold → normal, italic → roman, serif/mono → sans.

    Returns ``None`` if no font file can be found.
    """
    if user_font_path and Path(user_font_path).is_file():
        logger.debug("Using user-provided font: %s", user_font_path)
        return user_font_path

    category, weight, style = parse_font_name(base_font)
    logger.debug("Parsed %s → (%s, %s, %s)", base_font, category, weight, style)

    platform = _current_platform()
    fonts = _SYSTEM_FONTS.get(platform, {})

    # Try exact match, then degrade
    for cat, w, s in _degradation_chain(category, weight, style):
        path = fonts.get((cat, w, s))
        if path and Path(path).is_file():
            logger.debug("Resolved %s → %s", base_font, path)
            return path

    logger.warning("No system font found for %s on %s", base_font, platform)
    return None


def _degradation_chain(
    category: str, weight: str, style: str
) -> list[tuple[str, str, str]]:
    """Return lookup keys in degradation order."""
    chain: list[tuple[str, str, str]] = []

    # Exact
    chain.append((category, weight, style))

    # Drop style first (italic → roman)
    if style != "roman":
        chain.append((category, weight, "roman"))

    # Drop weight (bold → normal)
    if weight != "normal":
        chain.append((category, "normal", style))
        if style != "roman":
            chain.append((category, "normal", "roman"))

    # Fallback to sans if not already sans
    if category != "sans":
        chain.append(("sans", weight, style))
        if style != "roman":
            chain.append(("sans", weight, "roman"))
        if weight != "normal":
            chain.append(("sans", "normal", style))
            if style != "roman":
                chain.append(("sans", "normal", "roman"))
        else:
            chain.append(("sans", "normal", "roman"))

    return chain
