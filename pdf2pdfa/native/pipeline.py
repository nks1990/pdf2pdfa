"""Canonical end-to-end pipeline for the fully owned PDF/A engine.

The pipeline has no external executable, validator, PDF library, image library
or font library dependency. Every output is parsed, repaired, validated and
(optionally) fidelity-checked by code in :mod:`pdf2pdfa.native` before atomic
publication.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Literal

from .fidelity import NativeFidelityChecker, SemanticFidelityReport
from .font_embed import FontEmbeddingReport, FontProgramMap, embed_missing_fonts
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .pdfa import NativePDFAValidator, ValidationReport, policy
from .repair import RepairPlan, UnsupportedNativeRepairError
from .repair_owned import OwnedRepairEngine, flatten_page_numbers
from .security import InvalidPasswordError, SecurePDFDocument
from .structure import resolve, walk_reachable_objects
from .document import PDFDocument
from .visual_fidelity import NativeVisualFidelityChecker, VisualFidelityReport
from .writer import PDFWriter


FidelityMode = Literal["off", "semantic", "visual", "auto"]
FidelityReport = SemanticFidelityReport | VisualFidelityReport


class OwnedPipelineError(RuntimeError):
    pass


class SignatureInvalidationError(OwnedPipelineError):
    pass


class InputLimitError(OwnedPipelineError):
    pass


class OwnedValidationError(OwnedPipelineError):
    pass


class OwnedFidelityError(OwnedPipelineError):
    pass


@dataclass(frozen=True, slots=True)
class OwnedConversionResult:
    output_path: Path
    level: str
    validation: ValidationReport
    fidelity: FidelityReport | None
    fidelity_mode: str
    plan: RepairPlan
    fonts: FontEmbeddingReport | None
    source_was_already_compliant: bool
    source_was_encrypted: bool
    engine: str = "pdf2pdfa-owned"


def _source_bytes(
    source: str | Path | bytes,
    max_input_bytes: int | None,
) -> tuple[bytes, Path | None]:
    if isinstance(source, bytes):
        data = source
        path = None
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise OwnedPipelineError(f"input is not a regular file: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise OwnedPipelineError(f"input PDF is empty: {path}")
        if max_input_bytes is not None and size > max_input_bytes:
            raise InputLimitError(
                f"input PDF is {size} bytes, exceeding configured limit {max_input_bytes}"
            )
        data = path.read_bytes()
    if not data:
        raise OwnedPipelineError("input PDF is empty")
    if max_input_bytes is not None and len(data) > max_input_bytes:
        raise InputLimitError(
            f"input PDF is {len(data)} bytes, exceeding configured limit {max_input_bytes}"
        )
    return data, path


def _name(doc: PDFDocument, value: PDFObject | None) -> str:
    try:
        value = resolve(doc, value)
    except Exception:
        return ""
    return value.value if isinstance(value, PDFName) else ""


def _has_applied_signature(doc: PDFDocument) -> bool:
    for _path, value in walk_reachable_objects(doc):
        dictionary = (
            value.dictionary
            if isinstance(value, PDFStream)
            else value
            if isinstance(value, PDFDict)
            else None
        )
        if dictionary is None:
            continue
        if _name(doc, dictionary.get("Type")) == "Sig":
            return True
        if _name(doc, dictionary.get("FT")) == "Sig" and dictionary.get("V") is not None:
            try:
                resolved = resolve(doc, dictionary.get("V"))
            except Exception:
                resolved = dictionary.get("V")
            if resolved is not None:
                return True
    return False


class OwnedPDFAPipeline:
    """Convert PDF to PDF/A using only repository-owned runtime code.

    Validation is mandatory. ``fidelity='auto'`` uses semantic invariants for
    structural repairs and automatically upgrades to native visual comparison
    when a repair intentionally changes page painting, such as PDF/A-1
    transparency flattening.
    """

    def __init__(
        self,
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
        if fidelity not in ("off", "semantic", "visual", "auto"):
            raise ValueError("fidelity must be off, semantic, visual or auto")
        if max_input_bytes is not None and max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        if transparency_dpi <= 0 or transparency_dpi > 2400:
            raise ValueError("transparency_dpi must be between 1 and 2400")
        self.fidelity_mode = fidelity
        self.max_input_bytes = max_input_bytes
        self.allow_signature_invalidation = allow_signature_invalidation
        self.allow_attachment_removal = allow_attachment_removal
        self.transparency_dpi = transparency_dpi
        self.visual_dpi = visual_dpi or transparency_dpi
        self.validator = NativePDFAValidator()
        self.semantic_fidelity = NativeFidelityChecker()
        self.visual_fidelity = NativeVisualFidelityChecker(
            dpi=self.visual_dpi,
            pixel_tolerance=visual_pixel_tolerance,
            max_mean_error=visual_max_mean_error,
            max_changed_pixel_ratio=visual_max_changed_pixel_ratio,
        )

    def _load_document(
        self,
        data: bytes,
        *,
        password: str | bytes | None,
    ) -> tuple[PDFDocument, bool]:
        probe = PDFDocument.open(data, repair=True)
        encrypted = "Encrypt" in probe.trailer
        if not encrypted:
            return probe, False
        if password is None:
            raise InvalidPasswordError("encrypted PDF requires a password")
        secure = SecurePDFDocument.open_secure(data, password, repair=True)
        secure.remove_encryption_for_write()
        return secure, True

    def _preprocess_fonts(
        self,
        doc: PDFDocument,
        *,
        font_paths: Iterable[str | Path | bytes] | None,
        font_directories: Iterable[str | Path] | None,
    ) -> FontEmbeddingReport | None:
        paths = list(font_paths or ())
        directories = list(font_directories or ())
        if not paths and not directories:
            return None
        programs = FontProgramMap()
        errors: list[str] = []
        for source in paths:
            try:
                programs.add(source)
            except Exception as exc:
                errors.append(str(exc))
        for directory in directories:
            errors.extend(programs.add_directory(directory))
        if errors:
            raise OwnedPipelineError("font program loading failed: " + "; ".join(errors))
        report = embed_missing_fonts(doc, programs)
        if report.unsupported:
            raise OwnedPipelineError(
                "font embedding could not be proven safe: " + "; ".join(report.unsupported)
            )
        return report

    @staticmethod
    def _serialize_working_document(doc: PDFDocument, level: str) -> bytes:
        target = policy(level)
        return PDFWriter(
            doc,
            version="1.4" if target.part == 1 else "1.7",
            reachable_only=True,
        ).to_bytes()

    def _effective_fidelity(self, plan: RepairPlan) -> str:
        if self.fidelity_mode != "auto":
            return self.fidelity_mode
        return "visual" if flatten_page_numbers(plan) else "semantic"

    def _fidelity_check(
        self,
        *,
        mode: str,
        semantic_baseline: bytes,
        visual_baseline: bytes,
        candidate: Path,
        plan: RepairPlan,
    ) -> FidelityReport | None:
        if mode == "off":
            return None
        if mode == "semantic":
            if flatten_page_numbers(plan):
                raise OwnedFidelityError(
                    "semantic fidelity cannot approve an intentional page-painting rewrite; "
                    "use fidelity='visual' or 'auto' for transparency flattening"
                )
            report = self.semantic_fidelity.compare(
                semantic_baseline,
                candidate,
                allow_attachment_changes=self.allow_attachment_removal,
            )
            if not report.passed:
                raise OwnedFidelityError(
                    "semantic fidelity gate rejected conversion: "
                    + "; ".join(
                        f"{difference.code}: {difference.message}"
                        for difference in report.differences
                    )
                )
            return report
        if mode == "visual":
            report = self.visual_fidelity.compare(visual_baseline, candidate)
            if not report.passed:
                raise OwnedFidelityError(
                    "visual fidelity gate rejected conversion: "
                    + "; ".join(report.differences)
                )
            return report
        raise OwnedFidelityError(f"unknown fidelity mode {mode!r}")

    def convert(
        self,
        source: str | Path | bytes,
        destination: str | Path,
        *,
        level: str = "2b",
        password: str | bytes | None = None,
        font_paths: Iterable[str | Path | bytes] | None = None,
        font_directories: Iterable[str | Path] | None = None,
    ) -> OwnedConversionResult:
        target = policy(level)
        data, source_path = _source_bytes(source, self.max_input_bytes)
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Already-conforming unencrypted input is the strongest fidelity case:
        # byte-for-byte passthrough. Validation is still performed by us first.
        raw_probe = PDFDocument.open(data, repair=True)
        encrypted_source = "Encrypt" in raw_probe.trailer
        if not encrypted_source:
            initial = self.validator.validate(data, target.level)
            if initial.compliant:
                if source_path is not None and source_path == destination:
                    return OwnedConversionResult(
                        destination,
                        target.level,
                        initial,
                        None,
                        "passthrough",
                        RepairPlan(target.level, operations=["byte-for-byte passthrough"]),
                        None,
                        True,
                        False,
                    )
                with tempfile.TemporaryDirectory(
                    prefix="pdf2pdfa-owned-", dir=destination.parent
                ) as tempdir_name:
                    candidate = Path(tempdir_name) / "candidate.pdf"
                    candidate.write_bytes(data)
                    os.replace(candidate, destination)
                return OwnedConversionResult(
                    destination,
                    target.level,
                    initial,
                    None,
                    "passthrough",
                    RepairPlan(target.level, operations=["byte-for-byte passthrough"]),
                    None,
                    True,
                    False,
                )

        doc, encrypted_source = self._load_document(data, password=password)
        if _has_applied_signature(doc) and not self.allow_signature_invalidation:
            raise SignatureInvalidationError(
                "input contains an applied digital signature; rewriting would invalidate it. "
                "Set allow_signature_invalidation=True only when invalidation is intentional."
            )

        # Semantic comparison can use the plaintext document before font
        # embedding because content/image semantics are unchanged by embedding.
        semantic_baseline = self._serialize_working_document(doc, target.level)
        font_report = self._preprocess_fonts(
            doc,
            font_paths=font_paths,
            font_directories=font_directories,
        )
        working_bytes = self._serialize_working_document(doc, target.level)

        repair = OwnedRepairEngine(
            allow_attachment_removal=self.allow_attachment_removal,
            transparency_dpi=self.transparency_dpi,
        )
        plan = repair.plan(working_bytes, target.level)
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "owned conversion has unresolved blockers: "
                + "; ".join(
                    f"{blocker.code}: {blocker.message}" for blocker in plan.blockers
                )
            )

        working_doc = PDFDocument.open(working_bytes, repair=False)
        repair.repair_document(working_doc, target.level, plan)
        effective_fidelity = self._effective_fidelity(plan)

        with tempfile.TemporaryDirectory(
            prefix="pdf2pdfa-owned-", dir=destination.parent
        ) as tempdir_name:
            tempdir = Path(tempdir_name)
            candidate = tempdir / "candidate.pdf"
            PDFWriter(
                working_doc,
                version="1.4" if target.part == 1 else "1.7",
                reachable_only=True,
            ).write(candidate)

            validation = self.validator.validate(candidate, target.level)
            if not validation.compliant:
                raise OwnedValidationError(
                    "owned validator rejected owned conversion candidate: "
                    + ", ".join(validation.failed_rules)
                )

            fidelity_report = self._fidelity_check(
                mode=effective_fidelity,
                semantic_baseline=semantic_baseline,
                # Visual baseline is after any explicit user-supplied font
                # embedding, because an unembedded/missing font has no fully
                # self-contained visual meaning to compare without substitution.
                visual_baseline=working_bytes,
                candidate=candidate,
                plan=plan,
            )
            os.replace(candidate, destination)

        return OwnedConversionResult(
            output_path=destination,
            level=target.level,
            validation=validation,
            fidelity=fidelity_report,
            fidelity_mode=effective_fidelity,
            plan=plan,
            fonts=font_report,
            source_was_already_compliant=False,
            source_was_encrypted=encrypted_source,
        )
