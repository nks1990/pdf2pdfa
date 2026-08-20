from __future__ import annotations

import json
from pathlib import Path
import tempfile

from pdf2pdfa import __version__
from pdf2pdfa.agent_protocol import MACHINE_SCHEMA_VERSION
from pdf2pdfa.cli import main
from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream


def _source(*, javascript: bool = False, applied_signature: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    contents = builder.add(PDFStream(PDFDict(), b"0 0 0 rg\n10 10 40 40 re f\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    catalog = PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref})
    if javascript:
        catalog["OpenAction"] = PDFDict(
            {"S": PDFName("JavaScript"), "JS": b"app.alert('x')"}
        )
    if applied_signature:
        signature = builder.add(
            PDFDict(
                {
                    "Type": PDFName("Sig"),
                    "ByteRange": [0, 1, 2, 3],
                    "Contents": b"synthetic-signature",
                }
            )
        )
        catalog["OwnedTestSignature"] = signature
    root = builder.add(catalog)
    builder.set_root(root)
    return builder.to_bytes()


def _assert_envelope(payload: dict[str, object], command: str, exit_code: int) -> None:
    assert payload["schema_version"] == MACHINE_SCHEMA_VERSION
    assert payload["pdf2pdfa_version"] == __version__
    assert payload["command"] == command
    assert payload["exit_code"] == exit_code


def test_agent_convert_json_has_versioned_success_envelope(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        source = root / "source.pdf"
        output = root / "archive.pdf"
        source.write_bytes(_source(javascript=True))

        assert main(["convert", str(source), str(output), "--level", "2b", "--json"]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        _assert_envelope(payload, "convert", 0)
        assert payload["ok"] is True
        assert payload["status"] == "converted"
        result = payload["result"]
        assert isinstance(result, dict)
        assert result["engine"] == "pdf2pdfa-owned"
        assert result["validation"]["compliant"] is True


def test_agent_validate_noncompliant_is_structured_invalid_not_execution_error(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        source = Path(tempdir_name) / "source.pdf"
        source.write_bytes(_source())

        assert main(["validate", str(source), "--level", "2b", "--json"]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        _assert_envelope(payload, "validate", 1)
        assert payload["ok"] is False
        assert payload["status"] == "invalid"
        assert "error" not in payload
        result = payload["result"]
        assert isinstance(result, dict)
        assert result["compliant"] is False
        assert result["engine"] == "pdf2pdfa-owned"


def test_agent_inspect_blocker_is_structured_blocked(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        source = Path(tempdir_name) / "signed.pdf"
        source.write_bytes(_source(applied_signature=True))

        assert main(["inspect", str(source), "--level", "2b", "--json"]) == 2
        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        _assert_envelope(payload, "inspect", 2)
        assert payload["ok"] is False
        assert payload["status"] == "blocked"
        result = payload["result"]
        assert isinstance(result, dict)
        assert result["repairable"] is False
        blockers = result["plan"]["blockers"]
        assert any(item["code"] == "applied-signature" for item in blockers)


def test_agent_json_runtime_error_has_stable_code_and_no_human_stderr(capsys):
    missing = str(Path(tempfile.gettempdir()) / "pdf2pdfa-agent-definitely-missing.pdf")
    assert main(["validate", missing, "--level", "2b", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""

    payload = json.loads(captured.out)
    _assert_envelope(payload, "validate", 2)
    assert payload["ok"] is False
    assert payload["status"] == "invalid_input"
    error = payload["error"]
    assert error["code"] == "INPUT_NOT_FOUND"
    assert error["category"] == "invalid_input"
    assert error["retryable"] is False


def test_agent_json_usage_error_is_structured(capsys):
    assert main(["convert", "input.pdf", "output.pdf", "--backend", "ghostscript", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    _assert_envelope(payload, "convert", 2)
    assert payload["ok"] is False
    assert payload["status"] == "usage_error"
    assert payload["error"]["code"] == "USAGE_ERROR"


def test_agent_batch_partial_failure_keeps_per_item_machine_errors(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        good = root / "good.pdf"
        missing = root / "missing.pdf"
        good.write_bytes(_source())

        assert main(["batch", str(good), str(missing), "--level", "2b", "--json"]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        _assert_envelope(payload, "batch", 1)
        assert payload["ok"] is False
        assert payload["status"] == "partial_failure"
        result = payload["result"]
        assert result["failures"] == 1
        failed = [item for item in result["results"] if not item["ok"]]
        assert len(failed) == 1
        assert failed[0]["error"]["code"] == "INPUT_NOT_FOUND"
        assert failed[0]["status"] == "invalid_input"


def test_non_json_errors_remain_human_readable(capsys):
    missing = str(Path(tempfile.gettempdir()) / "pdf2pdfa-agent-definitely-missing-text.pdf")
    assert main(["validate", missing, "--level", "2b"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR:")
