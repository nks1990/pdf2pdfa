"""Adaptive PDF/A conversion orchestration.

The orchestrator is deliberately conservative: it preserves already-valid
files, uses pikepdf only for object-level repairs proven safe by preflight, and
falls back to a full Ghostscript rewrite for features that require rendering or
reconstruction.  When validation is requested, no candidate is published until
veraPDF confirms the requested flavour.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Literal

from .backends import (
    BackendUnavailableError,
    ConversionBackendError,
    GhostscriptBackend,
    PikePDFBackend,
)
from .model import PreflightReport
from .preflight import analyze_pdf
from .profiles import get_policy
from .validator import ValidationResult, VeraPDFValidator


BackendChoice = Literal["auto", "pikepdf", "ghostscript"]


class ConversionError(RuntimeError):
    pass


class SignatureInvalidationError(ConversionError):
    pass


@dataclass(frozen=True, slots=True)
class ConversionResult:
    output_path: Path
    level: str
    backend: str
    preflight: PreflightReport
    validation: ValidationResult | None
    fallback_used: bool = False
    source_was_already_compliant: bool = False


def _claim_matches(report: PreflightReport, level: str) -> bool:
    claim = report.features.get("existing_pdfa_claim") or {}
    return (
        str(claim.get("part", "")) == level[0]
        and str(claim.get("conformance", "")).upper() == level[1].upper()
    )


def _requires_full_rewrite(report: PreflightReport) -> bool:
    repair_codes = {
        "encryption",
        "javascript",
        "embedded_files",
        "transparency",
        "digital_signature",
    }
    if any(issue.code in repair_codes for issue in report.issues):
        return True
    if int(report.features.get("type0_fonts", 0) or 0) > 0:
        return True
    if int(report.features.get("complex_fonts", 0) or 0) > 0:
        return True
    return False


class ConversionOrchestrator:
    def __init__(
        self,
        *,
        backend: BackendChoice = "auto",
        validate: bool = False,
        allow_signature_invalidation: bool = False,
        ghostscript_executable: str | None = None,
        verapdf_executable: str = "verapdf",
        timeout: int = 300,
    ) -> None:
        if backend not in ("auto", "pikepdf", "ghostscript"):
            raise ValueError("backend must be one of: auto, pikepdf, ghostscript")
        self.backend = backend
        self.validate_output = validate
        self.allow_signature_invalidation = allow_signature_invalidation
        self.fast = PikePDFBackend()
        self.full = GhostscriptBackend(ghostscript_executable, timeout=timeout)
        self.validator = VeraPDFValidator(verapdf_executable, timeout=timeout)

    def _validate(self, candidate: Path, level: str) -> ValidationResult | None:
        if not self.validate_output:
            return None
        result = self.validator.validate(candidate, level)
        return result

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        level: str = "1b",
        icc_profile: str | Path | None = None,
        font_path: str | Path | None = None,
    ) -> ConversionResult:
        policy = get_policy(level)
        input_path = Path(input_path).resolve()
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        report = analyze_pdf(input_path, policy.level)
        if report.features.get("signed") and not self.allow_signature_invalidation:
            raise SignatureInvalidationError(
                "Input contains a digital signature. Conversion would invalidate it; "
                "set allow_signature_invalidation=True only if that is intentional."
            )

        # A validated already-compliant PDF is the safest possible conversion:
        # do not rewrite it at all.
        if self.validate_output and _claim_matches(report, policy.level):
            existing = self.validator.validate(input_path, policy.level)
            if existing.compliant:
                with tempfile.TemporaryDirectory(prefix="pdf2pdfa-", dir=output_path.parent) as tempdir:
                    candidate = Path(tempdir) / "candidate.pdf"
                    shutil.copy2(input_path, candidate)
                    os.replace(candidate, output_path)
                return ConversionResult(
                    output_path=output_path,
                    level=policy.level,
                    backend="passthrough",
                    preflight=report,
                    validation=existing,
                    source_was_already_compliant=True,
                )

        rewrite_required = _requires_full_rewrite(report)
        if self.backend == "pikepdf" and rewrite_required:
            raise ConversionError(
                "Preflight found features that cannot be repaired safely by the pikepdf fast path"
            )

        if self.backend == "ghostscript":
            selected = self.full
        elif self.backend == "pikepdf":
            selected = self.fast
        else:
            selected = self.full if rewrite_required else self.fast

        if not selected.available():
            raise BackendUnavailableError(
                f"Selected backend '{selected.name}' is unavailable"
            )

        fallback_used = False
        with tempfile.TemporaryDirectory(prefix="pdf2pdfa-", dir=output_path.parent) as tempdir:
            candidate = Path(tempdir) / "candidate.pdf"
            try:
                selected.convert(
                    input_path,
                    candidate,
                    level=policy.level,
                    icc_profile=icc_profile,
                    font_path=font_path,
                )
            except (ConversionBackendError, BackendUnavailableError):
                if self.backend != "auto" or selected is self.full or not self.full.available():
                    raise
                selected = self.full
                fallback_used = True
                selected.convert(
                    input_path,
                    candidate,
                    level=policy.level,
                    icc_profile=icc_profile,
                    font_path=font_path,
                )

            validation = self._validate(candidate, policy.level)
            if validation is not None and not validation.compliant:
                if self.backend == "auto" and selected is self.fast and self.full.available():
                    selected = self.full
                    fallback_used = True
                    candidate.unlink(missing_ok=True)
                    selected.convert(
                        input_path,
                        candidate,
                        level=policy.level,
                        icc_profile=icc_profile,
                        font_path=font_path,
                    )
                    validation = self._validate(candidate, policy.level)
                if validation is not None and not validation.compliant:
                    failed = f"{validation.failed_checks} veraPDF check(s) failed"
                    raise ConversionError(
                        f"Candidate is not compliant with PDF/A-{policy.level}: {failed}"
                    )

            if not candidate.is_file() or candidate.stat().st_size == 0:
                raise ConversionError("Conversion backend did not produce a PDF candidate")
            os.replace(candidate, output_path)

        return ConversionResult(
            output_path=output_path,
            level=policy.level,
            backend=selected.name,
            preflight=report,
            validation=validation,
            fallback_used=fallback_used,
        )
