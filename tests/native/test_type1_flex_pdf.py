from __future__ import annotations

from pathlib import Path

import pytest

from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline
from pdf2pdfa.native.page_render import RenderingError

from tests.native.test_type1_core import _notdef, _pfa, _pfb, _private
from tests.native.test_type1_flex import _flex_glyph, _othersubr
from tests.native.test_type1_pdf_render import _pdf, _type1_font


def _program(*, pfb: bool = False, glyph: bytes | None = None) -> bytes:
    private = _private({".notdef": _notdef(), "A": glyph or _flex_glyph()})
    return _pfb(private) if pfb else _pfa(private)


def _flex_pdf(*, pfb: bool = False, transparent: bool = False, glyph: bytes | None = None) -> bytes:
    font = _type1_font(_program(pfb=pfb, glyph=glyph))
    extra = None
    prefix = b""
    if transparent:
        extra = PDFDict(
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
        extra_resources=extra,
    )


def _dark_pixel_count(page) -> int:
    rgb = page.rgb_bytes()
    return sum(
        1
        for offset in range(0, len(rgb), 3)
        if min(rgb[offset], rgb[offset + 1], rgb[offset + 2]) < 200
    )


@pytest.mark.parametrize("pfb", [False, True])
def test_pdf_renderer_paints_standard_flex_from_pfa_and_pfb(pfb: bool):
    page = render_page_full(_flex_pdf(pfb=pfb), dpi=72)
    assert _dark_pixel_count(page) > 30


def test_unknown_mm_othersubr_remains_fail_closed_in_pdf_renderer():
    from tests.native.test_type1_core import _num

    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _othersubr(20, (1, 2)),
            b"\x0e",
        ]
    )
    with pytest.raises(RenderingError, match="OtherSubr 20"):
        render_page_full(_flex_pdf(glyph=glyph), dpi=72)


def test_pdfa1_pipeline_flattens_transparent_type1_flex_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "type1-flex-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_flex_pdf(transparent=True), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
