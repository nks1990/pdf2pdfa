"""Owned, pure-Python PDF engine used by pdf2pdfa.

This package deliberately depends only on the Python standard library.  It is
separate from the legacy pikepdf-backed implementation while the native engine
is brought to feature parity.
"""

from .document import PDFDocument, PDFParseError
from .objects import PDFName, PDFRef, PDFStream
from .writer import PDFWriter

__all__ = [
    "PDFDocument",
    "PDFName",
    "PDFParseError",
    "PDFRef",
    "PDFStream",
    "PDFWriter",
]
