from pathlib import Path

import pikepdf
from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / "data"


def test_metadata_preserves_creator_and_records_converter(tmp_path):
    input_pdf = DATA_DIR / "sample.pdf"
    output_pdf = tmp_path / "output.pdf"

    Converter().convert(str(input_pdf), str(output_pdf))

    with pikepdf.Pdf.open(str(output_pdf)) as pdf:
        info = pdf.docinfo
        # Conversion must not falsify the original authoring provenance.
        assert pikepdf.Name.Creator in info
        assert str(info[pikepdf.Name.Creator])
        assert "pdf2pdfa" in str(info[pikepdf.Name.Producer])
