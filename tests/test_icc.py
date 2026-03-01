"""Tests for ICC profile embedding."""
from pathlib import Path

import pikepdf
from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_icc_n_on_stream(tmp_path):
    """Verify /N is present on the ICC stream, not on the OutputIntent dict."""
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    output_intents = pdf.Root.OutputIntents
    assert len(output_intents) >= 1

    outi = output_intents[0]
    # /N must NOT be on the OutputIntent dictionary
    assert '/N' not in outi

    # /N must be on the DestOutputProfile stream
    dest_profile = outi['/DestOutputProfile']
    assert '/N' in dest_profile.stream_dict
    n_val = int(dest_profile.stream_dict['/N'])
    assert n_val == 3  # sRGB has 3 components


def test_icc_registry_name(tmp_path):
    """Verify /RegistryName is present on the OutputIntent."""
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    outi = pdf.Root.OutputIntents[0]
    assert '/RegistryName' in outi
    assert str(outi['/RegistryName']) == 'http://www.color.org'
