"""PDF to PDF/A conversion library (PDF/A-1b, 2b and 3b)."""

from importlib.metadata import PackageNotFoundError, version

from .converter import Converter
from .orchestrator import ConversionResult

try:
    __version__ = version("pdf2pdfa")
except PackageNotFoundError:  # Source tree without installed package metadata.
    __version__ = "0+unknown"

__all__ = ["Converter", "ConversionResult", "__version__"]
