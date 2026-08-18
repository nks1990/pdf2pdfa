from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from fontTools.ttLib import TTFont

from pdf2pdfa.converter import Converter
from pdf2pdfa.fonts import _WIN_ANSI_TO_UNICODE

DATA_DIR = Path(__file__).parent / "data"


def _converted(tmp_path):
    output = tmp_path / "output.pdf"
    Converter().convert(str(DATA_DIR / "sample.pdf"), str(output))
    return output


def _embedded_ps_name(tt: TTFont) -> str:
    preferred = tt["name"].getName(6, 3, 1, 0x409)
    if preferred is not None:
        return preferred.toUnicode().strip().replace(" ", "")
    for record in tt["name"].names:
        if record.nameID == 6:
            return record.toUnicode().strip().replace(" ", "")
    raise AssertionError("embedded font has no PostScript name")


def test_fonts_embedded(tmp_path):
    with pikepdf.Pdf.open(_converted(tmp_path)) as pdf:
        for page in pdf.pages:
            fonts = page.Resources.get("/Font")
            if not fonts:
                continue
            for name in fonts:
                descriptor = fonts[name].get("/FontDescriptor")
                if descriptor is None:
                    continue
                assert any(
                    key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
                )


def test_font_widths_not_uniform(tmp_path):
    with pikepdf.Pdf.open(_converted(tmp_path)) as pdf:
        fonts = pdf.pages[0].Resources.get("/Font")
        assert fonts is not None
        font = fonts[next(iter(fonts))]
        widths = list(font["/Widths"])
        assert len(widths) == 224
        assert len(set(widths)) > 1


def test_font_widths_match_program(tmp_path):
    """Verify /Widths arrays match the actual embedded font program metrics."""
    with pikepdf.Pdf.open(_converted(tmp_path)) as pdf:
        fonts = pdf.pages[0].Resources.get("/Font")
        if not fonts:
            return

        for resource_name in fonts:
            font = fonts[resource_name]
            descriptor = font.get("/FontDescriptor")
            if descriptor is None or "/FontFile2" not in descriptor:
                continue

            font_data = bytes(descriptor["/FontFile2"].read_bytes())
            with TTFont(io.BytesIO(font_data)) as tt:
                cmap = tt.getBestCmap() or {}
                hmtx = tt["hmtx"].metrics
                upem = tt["head"].unitsPerEm
                notdef = hmtx.get(".notdef", (upem, 0))[0]

                pdf_widths = [int(width) for width in font["/Widths"]]
                first_char = int(font["/FirstChar"])
                for index, pdf_width in enumerate(pdf_widths):
                    code = first_char + index
                    uni = _WIN_ANSI_TO_UNICODE.get(code, code)
                    glyph = cmap.get(uni, ".notdef")
                    advance = hmtx.get(glyph, (notdef, 0))[0]
                    expected = int(round(advance * 1000 / upem))
                    assert pdf_width == expected, (
                        f"Width mismatch at code {code}: PDF={pdf_width}, font={expected}"
                    )


def test_pdf_font_names_match_embedded_program(tmp_path):
    """Never claim Helvetica/Times/etc. while embedding a different TTF program."""
    with pikepdf.Pdf.open(_converted(tmp_path)) as pdf:
        fonts = pdf.pages[0].Resources.get("/Font")
        assert fonts is not None
        for resource_name in fonts:
            font = fonts[resource_name]
            descriptor = font.get("/FontDescriptor")
            if descriptor is None or "/FontFile2" not in descriptor:
                continue

            with TTFont(io.BytesIO(descriptor["/FontFile2"].read_bytes())) as tt:
                actual_name = _embedded_ps_name(tt)

            assert str(font["/BaseFont"]) == f"/{actual_name}"
            assert str(descriptor["/FontName"]) == f"/{actual_name}"
