from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pdf2pdfa.backends.base import BackendResult, ConversionBackendError
from pdf2pdfa.cli import cli
from pdf2pdfa.orchestrator import ConversionError, ConversionOrchestrator
from pdf2pdfa.validator import ValidationResult
from tests.fixtures import blank_pdf


class _FailingBackend:
    name = "pikepdf"

    def available(self):
        return True

    def convert(self, input_path, output_path, **kwargs):
        Path(output_path).write_bytes(b"partial-candidate")
        raise ConversionBackendError("simulated backend failure")


class _WorkingBackend:
    name = "pikepdf"

    def available(self):
        return True

    def convert(self, input_path, output_path, **kwargs):
        Path(output_path).write_bytes(b"candidate")
        return BackendResult(self.name, Path(output_path))


class _RejectingValidator:
    def validate(self, path, flavour):
        return ValidationResult(
            compliant=False,
            flavour=flavour,
            failed_checks=1,
            passed_checks=10,
        )


def test_backend_failure_does_not_overwrite_existing_output(tmp_path):
    source = blank_pdf(tmp_path / "source.pdf")
    output = tmp_path / "output.pdf"
    output.write_bytes(b"existing-output")

    orchestrator = ConversionOrchestrator(backend="pikepdf", validate=False)
    orchestrator.fast = _FailingBackend()

    with pytest.raises(ConversionBackendError):
        orchestrator.convert(source, output, level="2b")
    assert output.read_bytes() == b"existing-output"


def test_validation_failure_does_not_publish_candidate(tmp_path):
    source = blank_pdf(tmp_path / "source.pdf")
    output = tmp_path / "output.pdf"
    output.write_bytes(b"existing-output")

    orchestrator = ConversionOrchestrator(backend="pikepdf", validate=True)
    orchestrator.fast = _WorkingBackend()
    orchestrator.validator = _RejectingValidator()

    with pytest.raises(ConversionError, match="not compliant"):
        orchestrator.convert(source, output, level="2b")
    assert output.read_bytes() == b"existing-output"


def test_preflight_cli_json_is_machine_readable(tmp_path):
    source = blank_pdf(tmp_path / "source.pdf")
    result = CliRunner().invoke(cli, ["preflight", str(source), "--level", "2b", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["level"] == "2b"
    assert payload["features"]["encrypted"] is False
    assert isinstance(payload["issues"], list)


def test_cli_does_not_offer_plaintext_password_argument():
    result = CliRunner().invoke(cli, ["convert", "--help"])
    assert result.exit_code == 0
    assert "--password-file" in result.output
    assert "--password TEXT" not in result.output
    assert "PDF2PDFA_PASSWORD" in result.output
