from __future__ import annotations

from pathlib import Path

import pytest

from pdf2pdfa.backends.base import BackendResult, ConversionBackendError
from pdf2pdfa.model import PreflightReport, Severity
from pdf2pdfa.orchestrator import (
    ConversionOrchestrator,
    SignatureInvalidationError,
    _requires_full_rewrite,
)


def test_rewrite_required_for_profile_blockers():
    report = PreflightReport(level="1b")
    report.add("transparency", "flatten", Severity.ERROR, repairable=True)
    assert _requires_full_rewrite(report) is True


def test_rewrite_required_for_type0_fonts():
    report = PreflightReport(level="2b", features={"type0_fonts": 1})
    assert _requires_full_rewrite(report) is True


def test_fast_path_for_simple_report():
    report = PreflightReport(level="2b", features={"type0_fonts": 0, "complex_fonts": 0})
    assert _requires_full_rewrite(report) is False


def test_signed_pdf_requires_explicit_opt_in(monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    report = PreflightReport(level="1b", features={"signed": True})
    monkeypatch.setattr("pdf2pdfa.orchestrator.analyze_pdf", lambda *args, **kwargs: report)

    orchestrator = ConversionOrchestrator(validate=False)
    with pytest.raises(SignatureInvalidationError):
        orchestrator.convert(source, tmp_path / "out.pdf", level="1b")


def test_auto_falls_back_after_fast_backend_error(monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    destination = tmp_path / "out.pdf"
    report = PreflightReport(
        level="1b",
        features={"signed": False, "type0_fonts": 0, "complex_fonts": 0},
    )
    monkeypatch.setattr("pdf2pdfa.orchestrator.analyze_pdf", lambda *args, **kwargs: report)

    class BrokenFast:
        name = "pikepdf"
        def available(self):
            return True
        def convert(self, *args, **kwargs):
            raise ConversionBackendError("fast path failed")

    class WorkingFull:
        name = "ghostscript"
        def available(self):
            return True
        def convert(self, input_path, output_path, **kwargs):
            Path(output_path).write_bytes(b"candidate")
            return BackendResult(self.name, Path(output_path))

    orchestrator = ConversionOrchestrator(validate=False)
    orchestrator.fast = BrokenFast()
    orchestrator.full = WorkingFull()
    result = orchestrator.convert(source, destination, level="1b")

    assert destination.read_bytes() == b"candidate"
    assert result.backend == "ghostscript"
    assert result.fallback_used is True
