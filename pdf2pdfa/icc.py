"""ICC profile embedding utilities."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from typing import Dict

from pikepdf import Pdf, Name, Dictionary, Array, String

logger = logging.getLogger(__name__)


def embed_icc_profile(pdf: Pdf, icc_path: str) -> None:
    """Add an OutputIntent referencing *icc_path* to the PDF."""
    path = Path(icc_path)
    if not path.is_file():
        raise FileNotFoundError(f"ICC profile not found: {icc_path}")

    logger.debug("Embedding ICC profile %s", icc_path)
    data = path.read_bytes()
    try:
        text = data.decode('ascii')
        if all(ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r' for ch in text.strip()):
            data = base64.b64decode(text)
    except UnicodeDecodeError:
        pass
    icc_stream = pdf.make_stream(data)

    def _num_components(icc: bytes) -> int:
        if len(icc) < 20:
            return 3
        space = icc[16:20].decode('ascii', 'ignore')
        return {
            'RGB ': 3,
            'GRAY': 1,
            'CMYK': 4,
            'XYZ ': 3,
            'Lab ': 3,
        }.get(space, 3)

    outi = Dictionary({
        '/Type': Name('/OutputIntent'),
        '/S': Name('/GTS_PDFA1'),
        '/OutputConditionIdentifier': String('sRGB IEC61966-2.1'),
        '/DestOutputProfile': icc_stream,
        '/N': _num_components(data),
    })
    pdf.Root.OutputIntents = Array([outi])


