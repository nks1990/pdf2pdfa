from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, ObjectStreamMode, String

from pdf2pdfa.icc import embed_icc_profile
from pdf2pdfa.validator import NativePDFValidator


def _candidate(path: Path, level: str = "1b") -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    profile = Path(str(files("pdf2pdfa").joinpath("data/sRGB.icc.b64")))
    embed_icc_profile(pdf, profile)
    with pdf.open_metadata() as meta:
        meta["pdfaid:part"] = level[0]
        meta["pdfaid:conformance"] = level[1].upper()
        meta["dc:format"] = "application/pdf"
    kwargs = {"force_version": "1.4" if level == "1b" else "1.7", "encryption": False}
    if level == "1b":
        kwargs["object_stream_mode"] = ObjectStreamMode.disable
    pdf.save(path, **kwargs)
    pdf.close()
    return path


def test_native_validator_accepts_minimal_pdfa1b(tmp_path):
    path = _candidate(tmp_path / "ok.pdf", "1b")
    result = NativePDFValidator().validate(path, "1b")
    assert result.compliant, result.failures
    assert result.validator == "pdf2pdfa-native"


def test_native_validator_rejects_missing_xmp_and_output_intent(tmp_path):
    path = tmp_path / "plain.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.save(path)
    pdf.close()

    result = NativePDFValidator().validate(path, "1b")
    assert not result.compliant
    assert "metadata.required" in result.failed_rules
    assert "color.output_intent_required" in result.failed_rules


def test_native_validator_rejects_javascript(tmp_path):
    path = _candidate(tmp_path / "js.pdf", "2b")
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/OpenAction"] = Dictionary(
            {"/S": Name("/JavaScript"), "/JS": String("app.alert('x')")}
        )
        pdf.save(path)

    result = NativePDFValidator().validate(path, "2b")
    assert not result.compliant
    assert "actions.forbidden_type" in result.failed_rules


def test_pdfa2_accepts_plain_text_attachment(tmp_path):
    path = _candidate(tmp_path / "a2-text.pdf", "2b")
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(b"archival note\n")
        stream["/Type"] = Name("/EmbeddedFile")
        stream["/Subtype"] = Name("/text#2Fplain")
        spec = pdf.make_indirect(
            Dictionary(
                {
                    "/Type": Name("/Filespec"),
                    "/F": String("note.txt"),
                    "/UF": String("note.txt"),
                    "/EF": Dictionary({"/F": stream}),
                }
            )
        )
        pdf.Root["/Names"] = Dictionary(
            {"/EmbeddedFiles": Dictionary({"/Names": Array([String("note.txt"), spec])})}
        )
        pdf.save(path)

    result = NativePDFValidator().validate(path, "2b")
    assert result.compliant, result.failures


def test_pdfa2_rejects_arbitrary_attachment(tmp_path):
    path = _candidate(tmp_path / "a2-bin.pdf", "2b")
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(b"not archival")
        stream["/Type"] = Name("/EmbeddedFile")
        stream["/Subtype"] = Name("/application#2Foctet-stream")
        spec = pdf.make_indirect(
            Dictionary(
                {
                    "/Type": Name("/Filespec"),
                    "/F": String("data.bin"),
                    "/EF": Dictionary({"/F": stream}),
                }
            )
        )
        pdf.Root["/Names"] = Dictionary(
            {"/EmbeddedFiles": Dictionary({"/Names": Array([String("data.bin"), spec])})}
        )
        pdf.save(path)

    result = NativePDFValidator().validate(path, "2b")
    assert not result.compliant
    assert "pdfa2.embedded_file_type" in result.failed_rules


def test_pdfa3_requires_attachment_relationship(tmp_path):
    path = _candidate(tmp_path / "a3-bin.pdf", "3b")
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(b"arbitrary data")
        stream["/Type"] = Name("/EmbeddedFile")
        stream["/Subtype"] = Name("/application#2Foctet-stream")
        spec = pdf.make_indirect(
            Dictionary(
                {
                    "/Type": Name("/Filespec"),
                    "/F": String("data.bin"),
                    "/EF": Dictionary({"/F": stream}),
                }
            )
        )
        pdf.Root["/Names"] = Dictionary(
            {"/EmbeddedFiles": Dictionary({"/Names": Array([String("data.bin"), spec])})}
        )
        pdf.save(path)

    result = NativePDFValidator().validate(path, "3b")
    assert not result.compliant
    assert "pdfa3.af_relationship" in result.failed_rules


def test_pdfa3_accepts_attachment_with_relationship(tmp_path):
    path = _candidate(tmp_path / "a3-related.pdf", "3b")
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(b"arbitrary data")
        stream["/Type"] = Name("/EmbeddedFile")
        stream["/Subtype"] = Name("/application#2Foctet-stream")
        spec = pdf.make_indirect(
            Dictionary(
                {
                    "/Type": Name("/Filespec"),
                    "/F": String("data.bin"),
                    "/AFRelationship": Name("/Data"),
                    "/EF": Dictionary({"/F": stream}),
                }
            )
        )
        pdf.Root["/Names"] = Dictionary(
            {"/EmbeddedFiles": Dictionary({"/Names": Array([String("data.bin"), spec])})}
        )
        pdf.Root["/AF"] = Array([spec])
        pdf.save(path)

    result = NativePDFValidator().validate(path, "3b")
    assert result.compliant, result.failures
