"""End-to-end PDF/A conversion orchestrator using only owned engine code."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Literal

from .fidelity import NativeFidelityChecker, SemanticFidelityReport
from .font_embed import FontEmbeddingReport, FontProgramMap, embed_missing_fonts
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .pdfa import NativePDFAValidator, ValidationReport, policy
from .repair import (
    NativeRepairEngine,
    RepairPlan,
    UnsupportedNativeRepairError,
)
from .security import (
    InvalidPasswordError,
    SecurePDFDocument,
    UnsupportedSecurityHandlerError,
)
from .structure import resolve, walk_reachable_objects
from .document import PDFDocument
from .writer import PDFWriter


FidelityMode = Literal["off", "semantic"]


class NativeConversionError(RuntimeError):
    pass


class SignatureInvalidationError(NativeConversionError):
    pass


class InputLimitError(NativeConversionError):
    pass


class NativeValidationError(NativeConversionError):
    pass


class NativeFidelityError(NativeConversionError):
    pass


@dataclass(frozen=True, slots=True)
class NativeConversionResult:
    output_path: Path
    level: str
    validation: ValidationReport
    fidelity: SemanticFidelityReport | None
    plan: RepairPlan
    fonts: FontEmbeddingReport | None
    source_was_already_compliant: bool
    source_was_encrypted: bool
    engine: str = "pdf2pdfa-owned"


def _source_bytes(source: str | Path | bytes, max_input_bytes: int | None) -> tuple[bytes, Path | None]:
    if isinstance(source, bytes):
        data = source
        path = None
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise NativeConversionError(f"input is not a regular file: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise NativeConversionError(f"input PDF is empty: {path}")
        if max_input_bytes is not None and size > max_input_bytes:
            raise InputLimitError(
                f"input PDF is {size} bytes, exceeding configured limit {max_input_bytes}"
            )
        data = path.read_bytes()
    if not data:
        raise NativeConversionError("input PDF is empty")
    if max_input_bytes is not None and len(data) > max_input_bytes:
        raise InputLimitError(
            f"input PDF is {len(data)} bytes, exceeding configured limit {max_input_bytes}"
        )
    return data, path


def _dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _array(doc: PDFDocument, value: PDFObject | None) -> list[PDFObject] | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, list) else None


def _name(doc: PDFDocument, value: PDFObject | None) -> str:
    try:
        value = resolve(doc, value)
    except Exception:
        return ""
    return value.value if isinstance(value, PDFName) else ""


def _has_applied_signature(doc: PDFDocument) -> bool:
    # Signatures may be field dictionaries or standalone /Type /Sig objects.
    for _path, value in walk_reachable_objects(doc):
        dictionary = value.dictionary if isinstance(value, PDFStream) else value if isinstance(value, PDFDict) else None
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


def _empty_font_report() -> FontEmbeddingReport:
    return FontEmbeddingReport(0, 0, 0, (), ())


class OwnedConversionOrchestrator:
    """Convert and validate with zero external PDF/validation/runtime engines.

    Every published output is validated by ``NativePDFAValidator``. There is no
    ``validate=False`` escape hatch: an unvalidated candidate is never promoted
    to the requested output path.
    """

    def __init__(
        self,
        *,
        fidelity: FidelityMode = "semantic",
        max_input_bytes: int | None = None,
        allow_signature_invalidation: bool = False,
        allow_attachment_removal: bool = False,
    ) -> None:
        if fidelity not in ("off", "semantic"):
            raise ValueError("fidelity must be 'off' or 'semantic'")
        if max_input_bytes is not None and max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        self.fidelity_mode = fidelity
        self.max_input_bytes = max_input_bytes
        self.allow_signature_invalidation = allow_signature_invalidation
        self.allow_attachment_removal = allow_attachment_removal
        self.validator = NativePDFAValidator()
        self.fidelity_checker = NativeFidelityChecker()

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
            raise NativeConversionError("font program loading failed: " + "; ".join(errors))
        report = embed_missing_fonts(doc, programs)
        if report.unsupported:
            raise NativeConversionError(
                "font embedding could not be proven safe: " + "; ".join(report.unsupported)
            )
        return report

    def _serialize_working_document(self, doc: PDFDocument, level: str) -> bytes:
        target = policy(level)
        return PDFWriter(
            doc,
            version="1.4" if target.part == 1 else "1.7",
            reachable_only=True,
        ).to_bytes()

    def convert(
        self,
        source: str | Path | bytes,
        destination: str | Path,
        *,
        level: str = "2b",
        password: str | bytes | None = None,
        font_paths: Iterable[str | Path | bytes] | None = None,
        font_directories: Iterable[str | Path] | None = None,
    ) -> NativeConversionResult:
        target = policy(level)
        data, source_path = _source_bytes(source, self.max_input_bytes)
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Preserve already conforming, unencrypted files byte-for-byte. This is
        # the only conversion path that can also preserve an applied signature.
        initial = self.validator.validate(data, target.level)
        raw_probe = PDFDocument.open(data, repair=True)
        encrypted_source = "Encrypt" in raw_probe.trailer
        if initial.compliant and not encrypted_source:
            if source_path is not None and source_path == destination:
                return NativeConversionResult(
                    destination,
                    target.level,
                    initial,
                    None,
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
            return NativeConversionResult(
                destination,
                target.level,
                initial,
                None,
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

        # The fidelity baseline is plaintext but otherwise pre-repair. Font
        # embedding does not alter content streams and is therefore allowed to
        # occur after this snapshot.
        baseline_bytes = self._serialize_working_document(doc, target.level)
        font_report = self._preprocess_fonts(
            doc,
            font_paths=font_paths,
            font_directories=font_directories,
        )
        working_bytes = self._serialize_working_document(doc, target.level)

        plan = NativeRepairEngine(
            allow_attachment_removal=self.allow_attachment_removal
        ).plan(working_bytes, target.level)
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "owned conversion has unresolved blockers: "
                + "; ".join(
                    f"{blocker.code}: {blocker.message}" for blocker in plan.blockers
                )
            )

        working_doc = PDFDocument.open(working_bytes, repair=False)
        repair = NativeRepairEngine(
            allow_attachment_removal=self.allow_attachment_removal
        )
        repair.repair_document(working_doc, target.level, plan)

        fidelity_report: SemanticFidelityReport | None = None
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
                raise NativeValidationError(
                    "owned validator rejected owned conversion candidate: "
                    + ", ".join(validation.failed_rules)
                )

            if self.fidelity_mode == "semantic":
                fidelity_report = self.fidelity_checker.compare(
                    baseline_bytes,
                    candidate,
                    allow_attachment_changes=self.allow_attachment_removal,
                )
                if not fidelity_report.passed:
                    raise NativeFidelityError(
                        "semantic fidelity gate rejected conversion: "
                        + "; ".join(
                            f"{difference.code}: {difference.message}"
                            for difference in fidelity_report.differences
                        )
                    )

            os.replace(candidate, destination)

        return NativeConversionResult(
            output_path=destination,
            level=target.level,
            validation=validation,
            fidelity=fidelity_report,
            plan=plan,
            fonts=font_report,
            source_was_already_compliant=False,
            source_was_encrypted=encrypted_source,
        )
