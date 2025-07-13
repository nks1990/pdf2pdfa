from pathlib import Path

import pikepdf
from lxml import etree

from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_metadata_contains_pdfa_fields(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    metadata = pdf.Root.Metadata.read_bytes()
    root = etree.fromstring(metadata)
    ns = {
        'pdfaid': 'http://www.aiim.org/pdfa/ns/id/'
    }
    part = root.find('.//pdfaid:part', namespaces=ns)
    conf = root.find('.//pdfaid:conformance', namespaces=ns)
    assert part is not None and part.text == '1'
    assert conf is not None and conf.text == 'B'
