from __future__ import annotations

import json
from pathlib import Path
import tempfile

from pdf2pdfa import Converter, __version__
from pdf2pdfa.cli import main
from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pipeline import InputLimitError
from tests.native.font_fixture import make_test_ttf


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
                    "Filter": PDFName("Adobe.PPKLite"),
                    "SubFilter": PDFName("adbe.pkcs7.detached"),
                    "ByteRange": [0, 1, 2, 3],
                    "Contents": b"synthetic-signature",
                }
            )
        )
        catalog["OwnedTestSignature"] = signature
    root = builder.add(catalog)
    builder.set_root(root)
    return builder.to_bytes()


def _unembedded_font_source() -> bytes:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Font"),
                "Subtype": PDFName("TrueType"),
                "BaseFont": PDFName("OwnedTestFont"),
                "Encoding": PDFName("WinAnsiEncoding"),
                "FirstChar": 65,
                "LastChar": 65,
                "Widths": [600],
            }
        )
    )
    contents = builder.add(PDFStream(PDFDict(), b"BT /F1 12 Tf (A) Tj ET\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F1": font_ref})}),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_converter_public_api_is_owned_and_validates_every_output():
    with tempfile.TemporaryDirectory() as tempdir_name:
        output = Path(tempdir_name) / "archive.pdf"
        converter = Converter(level="2b", fidelity="auto")
        result = converter.convert(_source(javascript=True), output)
        assert result.engine == "pdf2pdfa-owned"
        assert result.validation.compliant
        assert result.fidelity is not None and result.fidelity.passed
        assert converter.validate(output).compliant
        assert b"JavaScript" not in output.read_bytes()


def test_inspection_exposes_owned_repair_plan_without_writing():
    converter = Converter(level="2b")
    result = converter.inspect(_source(javascript=True))
    assert not result.compliant
    assert result.repairable
    assert result.validation.engine == "pdf2pdfa-owned"
    assert any("action" in operation.lower() or "javascript" in operation.lower() for operation in result.plan.operations)


def test_inspection_simulates_explicit_font_preprocessing():
    source = _unembedded_font_source()
    without_font = Converter(level="2b").inspect(source)
    assert without_font.fonts is None

    with_font = Converter(level="2b").inspect(
        source,
        font_paths=[make_test_ttf()],
    )
    assert with_font.fonts is not None
    assert with_font.fonts.embedded == 1
    assert with_font.fonts.complete
    assert "OwnedTestFont" not in with_font.fonts.missing


def test_inspection_reports_applied_signature_rewrite_blocker():
    source = _source(applied_signature=True)
    blocked = Converter(level="2b").inspect(source)
    assert not blocked.repairable
    assert any(item.code == "applied-signature" for item in blocked.plan.blockers)

    intentional = Converter(
        level="2b",
        allow_signature_invalidation=True,
    ).inspect(source)
    assert all(item.code != "applied-signature" for item in intentional.plan.blockers)


def test_validate_enforces_input_limit_before_parsing_bytes():
    source = b"%PDF-1.7\n" + (b"x" * 1024)
    converter = Converter(level="2b", max_input_bytes=128)
    try:
        converter.validate(source)
    except InputLimitError as exc:
        assert "exceeding configured limit" in str(exc)
    else:
        raise AssertionError("validate unexpectedly ignored max_input_bytes")


def test_cli_validate_enforces_file_size_limit(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        source = Path(tempdir_name) / "oversized.pdf"
        source.write_bytes(b"%PDF-1.7\n" + (b"x" * (1024 * 1024)))
        assert main(
            [
                "validate",
                str(source),
                "--level",
                "2b",
                "--max-input-mib",
                "1",
            ]
        ) == 2
        captured = capsys.readouterr()
        assert "exceeding configured limit" in captured.err


def test_cli_inspect_signature_override_matches_converter_policy(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        source = Path(tempdir_name) / "signed.pdf"
        source.write_bytes(_source(applied_signature=True))

        assert main(["inspect", str(source), "--level", "2b", "--json"]) == 2
        blocked = json.loads(capsys.readouterr().out)["result"]
        assert blocked["repairable"] is False
        assert any(item["code"] == "applied-signature" for item in blocked["plan"]["blockers"])

        code = main(
            [
                "inspect",
                str(source),
                "--level",
                "2b",
                "--allow-signature-invalidation",
                "--json",
            ]
        )
        intentional = json.loads(capsys.readouterr().out)["result"]
        assert code in (0, 2)
        assert all(item["code"] != "applied-signature" for item in intentional["plan"]["blockers"])


def test_cli_inspect_reports_font_simulation(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        source = root / "font.pdf"
        font = root / "OwnedTestFont.ttf"
        source.write_bytes(_unembedded_font_source())
        font.write_bytes(make_test_ttf())

        code = main(
            [
                "inspect",
                str(source),
                "--level",
                "2b",
                "--font",
                str(font),
                "--json",
            ]
        )
        report = json.loads(capsys.readouterr().out)["result"]
        assert code in (0, 2)
        assert report["fonts"] is not None
        assert report["fonts"]["embedded"] == 1
        assert report["fonts"]["complete"] is True


def test_cli_version_matches_package_metadata(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("argparse --version must terminate with exit code 0")
    assert capsys.readouterr().out.strip() == f"pdf2pdfa {__version__}"


def test_cli_convert_and_validate_round_trip(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        source = root / "source.pdf"
        output = root / "archive.pdf"
        source.write_bytes(_source())

        assert main(["convert", str(source), str(output), "--level", "2b", "--json"]) == 0
        converted = json.loads(capsys.readouterr().out)["result"]
        assert converted["engine"] == "pdf2pdfa-owned"
        assert converted["validation"]["compliant"] is True

        assert main(["validate", str(output), "--level", "2b", "--json"]) == 0
        validated = json.loads(capsys.readouterr().out)["result"]
        assert validated["compliant"] is True
        assert validated["engine"] == "pdf2pdfa-owned"


def test_cli_has_no_external_backend_or_validator_options(capsys):
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        source = root / "source.pdf"
        output = root / "out.pdf"
        source.write_bytes(_source())
        assert main(["convert", str(source), str(output), "--backend", "ghostscript"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("ERROR:")
