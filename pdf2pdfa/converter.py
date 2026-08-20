"""Public facade for the repository-owned PDF/A engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .native.document import PDFDocument
from .native.font_embed import FontEmbeddingReport
from .native.pdfa import NativePDFAValidator, ValidationReport, policy
from .native.pipeline import (
    FidelityMode,
    InputLimitError,
    OwnedConversionResult,
    OwnedPDFAPipeline,
    _has_applied_signature,
)
from .native.repair import RepairPlan
from .native.repair_owned import OwnedRepairEngine


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """Conversion-oriented inspection using only the owned engine."""

    level: str
    validation: ValidationReport
    plan: RepairPlan
    encrypted: bool
    fonts: FontEmbeddingReport | None = None

    @property
    def compliant(self) -> bool:
        return self.validation.compliant and not self.encrypted

    @property
    def repairable(self) -> bool:
        return not self.plan.blockers


def _read(
    source: str | Path | bytes,
    max_input_bytes: int | None = None,
) -> bytes:
    if isinstance(source, bytes):
        data = source
        if not data:
            raise ValueError("input PDF is empty")
        if max_input_bytes is not None and len(data) > max_input_bytes:
            raise InputLimitError(
                f"input PDF is {len(data)} bytes, exceeding configured limit {max_input_bytes}"
            )
        return data

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"input PDF is empty: {path}")
    if max_input_bytes is not None and size > max_input_bytes:
        raise InputLimitError(
            f"input PDF is {size} bytes, exceeding configured limit {max_input_bytes}"
        )
    data = path.read_bytes()
    if not data:
        raise ValueError(f"input PDF is empty: {path}")
    # Recheck after the read to close the stat/read race if the file changed.
    if max_input_bytes is not None and len(data) > max_input_bytes:
        raise InputLimitError(
            f"input PDF is {len(data)} bytes, exceeding configured limit {max_input_bytes}"
        )
    return data


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
        if max_input_bytes is not None and max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        self.max_input_bytes = max_input_bytes
        self.allow_signature_invalidation = allow_signature_invalidation
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
        data = _read(source, self.max_input_bytes)
        return NativePDFAValidator().validate(data, policy(level or self.level).level)

    def inspect(
        self,
        source: str | Path | bytes,
        *,
        password: str | bytes | None = None,
        level: str | None = None,
        font_paths: Iterable[str | Path | bytes] | None = None,
        font_directories: Iterable[str | Path] | None = None,
    ) -> InspectionResult:
        """Dry-run the conversion preparation and return validation/repair blockers.

        Inspection follows the same pre-repair path as :meth:`convert`: it
        handles encryption, signature policy, explicit font preprocessing and
        profile-specific serialization before asking the repair engine for a
        plan. No destination is written.
        """
        target = policy(level or self.level)
        data = _read(source, self.max_input_bytes)

        raw_probe = PDFDocument.open(data, repair=True)
        encrypted_source = "Encrypt" in raw_probe.trailer
        if not encrypted_source:
            initial = NativePDFAValidator().validate(data, target.level)
            if initial.compliant:
                return InspectionResult(
                    target.level,
                    initial,
                    RepairPlan(target.level, operations=["byte-for-byte passthrough"]),
                    False,
                    None,
                )

        document, encrypted_source = self._pipeline._load_document(  # noqa: SLF001
            data,
            password=password,
        )
        signature_blocked = (
            _has_applied_signature(document)
            and not self.allow_signature_invalidation
        )

        font_report = self._pipeline._preprocess_fonts(  # noqa: SLF001
            document,
            font_paths=font_paths,
            font_directories=font_directories,
        )
        working = self._pipeline._serialize_working_document(  # noqa: SLF001
            document,
            target.level,
        )
        validation = NativePDFAValidator().validate(working, target.level)
        plan = OwnedRepairEngine(
            allow_attachment_removal=self.allow_attachment_removal,
            transparency_dpi=self.transparency_dpi,
        ).plan(working, target.level)

        if signature_blocked:
            plan.block(
                "applied-signature",
                "input contains an applied digital signature; rewriting would invalidate it",
            )

        return InspectionResult(
            target.level,
            validation,
            plan,
            encrypted_source,
            font_report,
        )

    def preflight(
        self,
        source: str | Path | bytes,
        *,
        password: str | bytes | None = None,
        font_paths: Iterable[str | Path | bytes] | None = None,
        font_directories: Iterable[str | Path] | None = None,
    ) -> InspectionResult:
        """Backward-friendly alias for :meth:`inspect`."""
        return self.inspect(
            source,
            password=password,
            font_paths=font_paths,
            font_directories=font_directories,
        )

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
