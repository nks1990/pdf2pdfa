from pathlib import Path

import pikepdf
from fontTools.ttLib import TTFont

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


def test_font_widths_match_program(tmp_path):
    """Verify /Widths array matches the actual font program metrics."""
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    from pdf2pdfa.converter import Converter
    from pdf2pdfa.fonts import _WIN_ANSI_TO_UNICODE

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    pdf = pikepdf.Pdf.open(str(output_pdf))
    page = pdf.pages[0]
    fonts = page.Resources.get('/Font')
    if not fonts:
        return

    for fname in fonts:
        font = fonts[fname]
        desc = font.get('/FontDescriptor')
        if desc is None or '/FontFile2' not in desc:
            continue

        # Extract the embedded font program
        font_data = bytes(desc['/FontFile2'].read_bytes())
        import io
        tt = TTFont(io.BytesIO(font_data))
        cmap = tt.getBestCmap()
        hmtx = tt['hmtx'].metrics
        upem = tt['head'].unitsPerEm

        pdf_widths = [int(w) for w in font['/Widths']]
        first_char = int(font['/FirstChar'])

        for i, pdf_w in enumerate(pdf_widths):
            code = first_char + i
            uni = _WIN_ANSI_TO_UNICODE.get(code, code)
            gname = cmap.get(uni, '.notdef')
            adv = hmtx.get(gname, hmtx.get('.notdef'))[0]
            expected = int(round(adv * 1000 / upem))
            assert pdf_w == expected, (
                f"Width mismatch at code {code}: PDF={pdf_w}, font={expected}"
            )
