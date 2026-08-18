"""Public PDF/A conversion facade."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .orchestrator import BackendChoice, ConversionOrchestrator, ConversionResult
from .preflight import analyze_pdf
from .profiles import get_policy


class Converter:
    """Convert PDFs to PDF/A-1b, PDF/A-2b or PDF/A-3b.

    ``backend='auto'`` preserves already-valid files, uses the conservative
    pikepdf fast path when preflight proves that safe, and falls back to
    Ghostscript when a full rewrite is required. ``validate=True`` makes
    veraPDF conformance a publication gate.
    """

    def __init__(
        self,
        icc_path: str | None = None,
        level: str = "1b",
        *,
        backend: BackendChoice = "auto",
        validate: bool = False,
        allow_signature_invalidation: bool = False,
        ghostscript_executable: str | None = None,
        verapdf_executable: str = "verapdf",
        timeout: int = 300,
        max_input_bytes: int | None = None,
    ) -> None:
        policy = get_policy(level)
        self.level = policy.level
        self.icc_path = icc_path or str(files(__package__).joinpath("data/sRGB.icc.b64"))
        self.max_input_bytes = max_input_bytes
        self._orchestrator = ConversionOrchestrator(
            backend=backend,
            validate=validate,
            allow_signature_invalidation=allow_signature_invalidation,
            ghostscript_executable=ghostscript_executable,
            verapdf_executable=verapdf_executable,
            timeout=timeout,
            max_input_bytes=max_input_bytes,
        )

    def preflight(
        self,
        input_path: str | Path,
        *,
        password: str | bytes | None = None,
    ):
        """Return a non-mutating profile-aware preflight report."""
        return analyze_pdf(
            input_path,
            self.level,
            password=password,
            max_input_bytes=self.max_input_bytes,
        )

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        icc_profile: str | Path | None = None,
        font_path: str | Path | None = None,
        *,
        password: str | bytes | None = None,
    ) -> ConversionResult:
        """Convert *input_path* and atomically publish *output_path*."""
        return self._orchestrator.convert(
            input_path,
            output_path,
            level=self.level,
            icc_profile=icc_profile or self.icc_path,
            font_path=font_path,
            password=password,
        )
