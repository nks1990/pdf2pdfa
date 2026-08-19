from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_type1_core import _notdef, _pfa, _private
from tests.native.test_type1_pdf_render import _pdf, _type1_font
from tests.native.test_type1_seac import _box, _seac


def _seac_pdf(*, transparent: bool = False) -> bytes:
    program = _pfa(
        _private(
            {
                ".notdef": _notdef(),
                "A": _box(sbx=0, width=600, x=100, y=100, w=300, h=400),
                "acute": _box(sbx=30, width=200, x=10, y=500, w=100, h=100),
                "Aacute": _seac(),
            }
        )
    )
    encoding = PDFDict(
        {
            "BaseEncoding": PDFName("WinAnsiEncoding"),
            "Differences": [65, PDFName("Aacute")],
        }
    )
    font = _type1_font(program, encoding=encoding)
    resources = None
    prefix = b""
    if transparent:
        resources = PDFDict(
            {
                "ExtGState": PDFDict(
                    {
                        "GS": PDFDict(
                            {
                                "Type": PDFName("ExtGState"),
                                "ca": 0.5,
                                "CA": 0.5,
                            }
                        )
                    }
                )
            }
        )
        prefix = b"/GS gs "
    return _pdf(
        font,
        prefix + b"BT /F 60 Tf 1 0 0 1 10 10 Tm <41> Tj ET\n",
        extra_resources=resources,
    )


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_pdf_type1_seac_renders_base_and_accent_original_charstrings():
    page = render_page_full(_seac_pdf(), dpi=72)
    base = _bottom_pixel(page, 20, 25)
    accent = _bottom_pixel(page, 28, 46)
    between = _bottom_pixel(page, 50, 46)
    assert base.r < 0.1 and base.g < 0.1 and base.b < 0.1
    assert accent.r < 0.1 and accent.g < 0.1 and accent.b < 0.1
    assert between.r > 0.9 and between.g > 0.9 and between.b > 0.9


def test_pdfa1_pipeline_flattens_transparent_type1_seac_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "type1-seac-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_seac_pdf(transparent=True), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
