from pathlib import Path

import pikepdf
from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_info_synced_from_xmp(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    info = pdf.docinfo

    # These fields should exist and be non-empty
    assert pikepdf.Name.Creator in info
    assert str(info[pikepdf.Name.Creator]) == 'pdf2pdfa'
    assert str(info[pikepdf.Name.Producer]).startswith('pikepdf')
