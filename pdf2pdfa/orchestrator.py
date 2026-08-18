"""Adaptive PDF/A conversion orchestration.

The orchestrator is deliberately conservative: it preserves already-valid
files, uses pikepdf only for object-level repairs proven safe by preflight, and
falls back to a full Ghostscript rewrite for features that require rendering or
reconstruction. When validation is requested, no candidate is published until
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
from .security import decrypt_to_file, validate_input_file
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
    source_was_encrypted: bool = False


def _claim_matches(report: PreflightReport, level: str) -> bool:
    claim = report.features.get("existing_pdfa_claim") or {}
    return (
        str(claim.get("part", "")) == level[0]
        and str(claim.get("conformance", "")).upper() == level[1].upper()
    )


def _requires_full_rewrite(report: PreflightReport) -> bool:
    # Encryption is intentionally absent: it is safely removed in-process to a
    # private temporary PDF before either backend sees the source.
    repair_codes = {
        "javascript",
        "embedded_files",
        "transparency",
        "digital_signature",
        "direct_device_color",
        "content_stream_parse",
    }
    if any(issue.code in repair_codes for issue in report.issues):
        return True
    if int(report.features.get("type0_fonts", 0) or 0) > 0:
        return True
    if int(report.features.get("complex_fonts", 0) or 0) > 0:
        return True

    # Assigning an arbitrary CMYK source profile is not a color conversion.
    # The fast path therefore handles explicit RGB resources only; CMYK is
    # delegated to Ghostscript where a real color-conversion strategy is used.
    device_spaces = report.features.get("device_color_spaces") or {}
    if int(device_spaces.get("/DeviceCMYK", 0) or 0) > 0:
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
        max_input_bytes: int | None = None,
    ) -> None:
        if backend not in ("auto", "pikepdf", "ghostscript"):
            raise ValueError("backend must be one of: auto, pikepdf, ghostscript")
        if max_input_bytes is not None and max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive when provided")
        self.backend = backend
        self.validate_output = validate
        self.allow_signature_invalidation = allow_signature_invalidation
        self.max_input_bytes = max_input_bytes
        self.fast = PikePDFBackend()
        self.full = GhostscriptBackend(ghostscript_executable, timeout=timeout)
        self.validator = VeraPDFValidator(verapdf_executable, timeout=timeout)

    def _validate(self, candidate: Path, level: str) -> ValidationResult | None:
        if not self.validate_output:
            return None
        return self.validator.validate(candidate, level)

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        level: str = "1b",
        icc_profile: str | Path | None = None,
        font_path: str | Path | None = None,
        password: str | bytes | None = None,
    ) -> ConversionResult:
        policy = get_policy(level)
        input_path = validate_input_file(input_path, max_bytes=self.max_input_bytes)
        output_path = Path(output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        report = analyze_pdf(
            input_path,
            policy.level,
            password=password,
            max_input_bytes=self.max_input_bytes,
        )
        encrypted = bool(report.features.get("encrypted"))
        if report.features.get("signed") and not self.allow_signature_invalidation:
            raise SignatureInvalidationError(
                "Input contains a digital signature. Conversion would invalidate it; "
                "set allow_signature_invalidation=True only if that is intentional."
            )

        # A validated already-compliant, unencrypted PDF is the safest possible
        # conversion: do not rewrite it at all.
        if self.validate_output and not encrypted and _claim_matches(report, policy.level):
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
                    source_was_encrypted=False,
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
            raise BackendUnavailableError(f"Selected backend '{selected.name}' is unavailable")

        fallback_used = False
        with tempfile.TemporaryDirectory(prefix="pdf2pdfa-", dir=output_path.parent) as tempdir_name:
            tempdir = Path(tempdir_name)
            backend_input = input_path
            if encrypted:
                # Password handling ends here. External backends only receive an
                # unencrypted private working copy and never see the password in
                # argv, environment variables or logs.
                backend_input = decrypt_to_file(
                    input_path,
                    tempdir / "decrypted-input.pdf",
                    password=password,
                )

            candidate = tempdir / "candidate.pdf"
            try:
                selected.convert(
                    backend_input,
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
                    backend_input,
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
                        backend_input,
                        candidate,
                        level=policy.level,
                        icc_profile=icc_profile,
                        font_path=font_path,
                    )
                    validation = self._validate(candidate, policy.level)
                if validation is not None and not validation.compliant:
                    raise ConversionError(
                        f"Candidate is not compliant with PDF/A-{policy.level}: "
                        f"{validation.failed_checks} veraPDF check(s) failed"
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
            source_was_encrypted=encrypted,
        )
