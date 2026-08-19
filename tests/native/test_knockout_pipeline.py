from __future__ import annotations

from pathlib import Path

import pytest

from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline

from tests.native.test_knockout_transparency import _knockout_pdf


@pytest.mark.parametrize("isolated", [True, False])
def test_pdfa1_pipeline_flattens_knockout_group_with_visual_fidelity(
    tmp_path: Path,
    isolated: bool,
):
    source = _knockout_pdf(isolated=isolated)
    output = tmp_path / ("knockout-isolated.pdf" if isolated else "knockout-nonisolated.pdf")

    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(source, output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
