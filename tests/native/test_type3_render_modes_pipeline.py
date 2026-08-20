from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline
from pdf2pdfa.native.structure import resolve, walk_pages
from pdf2pdfa.native.writer import PDFWriter

from tests.native.test_type3_render import _pdf


@pytest.mark.parametrize("render_mode", range(8))
def test_pdfa1_pipeline_preserves_type3_charproc_for_all_text_render_modes(
    tmp_path: Path,
    render_mode: int,
):
    source = _pdf(
        content=(
            b"1 0 0 rg\n"
            b"/GS gs\n"
            + f"BT /F3 60 Tf {render_mode} Tr 1 0 0 1 10 10 Tm (A) Tj ET\n".encode("ascii")
        )
    )

    # _pdf deliberately has no ExtGState. Add transparency with the owned COS
    # model and serialize it with the owned deterministic writer.
    doc = PDFDocument.open(source, repair=True)
    page = next(iter(walk_pages(doc)))
    resources = resolve(doc, page.get("Resources"))
    assert isinstance(resources, PDFDict)
    resources["ExtGState"] = PDFDict(
        {
            "GS": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    "ca": Decimal("0.5"),
                    "CA": Decimal("0.5"),
                }
            )
        }
    )
    transparent_source = PDFWriter(doc).to_bytes()

    output = tmp_path / f"type3-tr-{render_mode}-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(transparent_source, output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
