"""Adaptive PDF to PDF/A conversion library."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .converter import Converter
from .orchestrator import ConversionResult

try:
    __version__ = version("pdf2pdfa")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"

__all__ = ["Converter", "ConversionResult", "__version__"]
