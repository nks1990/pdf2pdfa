"""Dependency-free PDF/A conversion library backed by the owned engine."""

from importlib.metadata import PackageNotFoundError, version

from .converter import Converter, ConversionResult, InspectionResult
from .native.pdfa import ValidationReport

try:
    __version__ = version("pdf2pdfa")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "Converter",
    "ConversionResult",
    "InspectionResult",
    "ValidationReport",
    "__version__",
]
