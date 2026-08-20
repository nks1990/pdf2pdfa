from __future__ import annotations

import json
from pathlib import Path
import tempfile

from pdf2pdfa import Converter, __version__
from pdf2pdfa.cli import main
from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream


def _source(*, javascript: bool = False) -> bytes:
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
    root = builder.add(catalog)
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
        converted = json.loads(capsys.readouterr().out)
        assert converted["engine"] == "pdf2pdfa-owned"
        assert converted["validation"]["compliant"] is True

        assert main(["validate", str(output), "--level", "2b", "--json"]) == 0
        validated = json.loads(capsys.readouterr().out)
        assert validated["compliant"] is True
        assert validated["engine"] == "pdf2pdfa-owned"


def test_cli_has_no_external_backend_or_validator_options():
    # argparse must reject the removed architectural escape hatches.
    with tempfile.TemporaryDirectory() as tempdir_name:
        root = Path(tempdir_name)
        source = root / "source.pdf"
        output = root / "out.pdf"
        source.write_bytes(_source())
        try:
            main(["convert", str(source), str(output), "--backend", "ghostscript"])
        except SystemExit as exc:
            assert exc.code != 0
        else:
            raise AssertionError("removed --backend option unexpectedly accepted")
