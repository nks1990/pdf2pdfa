"""Public facade for the repository-owned PDF/A engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .native.document import PDFDocument
from .native.pdfa import NativePDFAValidator, ValidationReport, policy
from .native.pipeline import (
    FidelityMode,
    OwnedConversionResult,
    OwnedPDFAPipeline,
)
from .native.repair import RepairPlan
from .native.repair_owned import OwnedRepairEngine
from .native.security import InvalidPasswordError, SecurePDFDocument
from .native.writer import PDFWriter


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """Conversion-oriented inspection using only the owned engine."""

    level: str
    validation: ValidationReport
    plan: RepairPlan
    encrypted: bool

    @property
    def compliant(self) -> bool:
        return self.validation.compliant and not self.encrypted

    @property
    def repairable(self) -> bool:
        return not self.plan.blockers


def _read(source: str | Path | bytes) -> bytes:
    if isinstance(source, bytes):
        return source
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    if not data:
        raise ValueError(f"input PDF is empty: {path}")
    return data


def _plaintext_for_inspection(
    data: bytes,
    *,
    level: str,
    password: str | bytes | None,
) -> tuple[bytes, bool]:
    probe = PDFDocument.open(data, repair=True)
    encrypted = "Encrypt" in probe.trailer
    if not encrypted:
        return data, False
    if password is None:
        raise InvalidPasswordError("encrypted PDF requires a password for conversion inspection")
    secure = SecurePDFDocument.open_secure(data, password, repair=True)
    secure.remove_encryption_for_write()
    target = policy(level)
    return (
        PDFWriter(
            secure,
            version="1.4" if target.part == 1 else "1.7",
            reachable_only=True,
        ).to_bytes(),
        True,
    )


class Converter:
    """Convert to PDF/A-1b, PDF/A-2b or PDF/A-3b with owned code only.

    There is no backend selector and no optional validation switch. Parsing,
    repair, PDF/A validation, security handling and fidelity checking are all
    implemented in ``pdf2pdfa.native`` and every rewritten candidate must pass
    the owned validator before it can replace the requested output path.
    """

    def __init__(
        self,
        level: str = "2b",
        *,
        fidelity: FidelityMode = "auto",
        max_input_bytes: int | None = None,
        allow_signature_invalidation: bool = False,
        allow_attachment_removal: bool = False,
        transparency_dpi: int = 144,
        visual_dpi: int | None = None,
        visual_pixel_tolerance: int = 2,
        visual_max_mean_error: float = 1.0,
        visual_max_changed_pixel_ratio: float = 0.01,
    ) -> None:
        self.level = policy(level).level
        self.max_input_bytes = max_input_bytes
        self.allow_attachment_removal = allow_attachment_removal
        self.transparency_dpi = transparency_dpi
        self._pipeline = OwnedPDFAPipeline(
            fidelity=fidelity,
            max_input_bytes=max_input_bytes,
            allow_signature_invalidation=allow_signature_invalidation,
            allow_attachment_removal=allow_attachment_removal,
            transparency_dpi=transparency_dpi,
            visual_dpi=visual_dpi,
            visual_pixel_tolerance=visual_pixel_tolerance,
            visual_max_mean_error=visual_max_mean_error,
            visual_max_changed_pixel_ratio=visual_max_changed_pixel_ratio,
        )

    def validate(
        self,
        source: str | Path | bytes,
        *,
        level: str | None = None,
    ) -> ValidationReport:
        """Validate *source* with the owned PDF/A conformance engine."""
        data = _read(source)
        return NativePDFAValidator().validate(data, policy(level or self.level).level)

    def inspect(
        self,
        source: str | Path | bytes,
        *,
        password: str | bytes | None = None,
        level: str | None = None,
    ) -> InspectionResult:
        """Return conformance plus the exact owned repair plan, without writing."""
        target = policy(level or self.level)
        data = _read(source)
        if self.max_input_bytes is not None and len(data) > self.max_input_bytes:
            raise ValueError(
                f"input PDF is {len(data)} bytes, exceeding configured limit {self.max_input_bytes}"
            )
        working, encrypted = _plaintext_for_inspection(
            data,
            level=target.level,
            password=password,
        )
        validation = NativePDFAValidator().validate(working, target.level)
        plan = OwnedRepairEngine(
            allow_attachment_removal=self.allow_attachment_removal,
            transparency_dpi=self.transparency_dpi,
        ).plan(working, target.level)
        return InspectionResult(target.level, validation, plan, encrypted)

    def preflight(
        self,
        source: str | Path | bytes,
        *,
        password: str | bytes | None = None,
    ) -> InspectionResult:
        """Backward-friendly alias for :meth:`inspect`."""
        return self.inspect(source, password=password)

    def convert(
        self,
        source: str | Path | bytes,
        destination: str | Path,
        *,
        password: str | bytes | None = None,
        font_paths: Iterable[str | Path | bytes] | None = None,
        font_directories: Iterable[str | Path] | None = None,
    ) -> OwnedConversionResult:
        """Convert and atomically publish only after owned validation/fidelity gates."""
        return self._pipeline.convert(
            source,
            destination,
            level=self.level,
            password=password,
            font_paths=font_paths,
            font_directories=font_directories,
        )


ConversionResult = OwnedConversionResult
