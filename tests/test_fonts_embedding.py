from pathlib import Path

import pikepdf

DATA_DIR = Path(__file__).parent / 'data'


def test_fonts_embedded(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    from pdf2pdfa.converter import Converter

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    for page in pdf.pages:
        fonts = page.Resources.get('/Font')
        if not fonts:
            continue
        for name in fonts:
            font = fonts[name]
            descriptor = font.get('/FontDescriptor')
            if descriptor is None:
                continue
            assert any(k in descriptor for k in ('/FontFile', '/FontFile2', '/FontFile3'))


def test_font_widths_not_uniform(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    from pdf2pdfa.converter import Converter

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    page = pdf.pages[0]
    fonts = page.Resources.get('/Font')
    assert fonts is not None
    name = next(iter(fonts))
    font = fonts[name]
    widths = list(font['/Widths'])
    assert len(widths) == 224
    assert len(set(widths)) > 1
