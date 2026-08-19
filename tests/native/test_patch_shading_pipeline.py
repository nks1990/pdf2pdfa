from __future__ import annotations

from pathlib import Path

import pytest

from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline

from tests.native.test_mesh_shading45 import _pdf
from tests.native.test_patch_shading67 import _patch_stream


@pytest.mark.parametrize("shading_type", [6, 7])
def test_pdfa1_pipeline_flattens_transparent_patch_mesh_with_visual_fidelity(
    tmp_path: Path,
    shading_type: int,
):
    output = tmp_path / f"patch-{shading_type}-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(
        _pdf(_patch_stream(shading_type), alpha=0.5),
        output,
        level="1b",
    )

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
