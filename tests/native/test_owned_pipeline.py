from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedFidelityError, OwnedPDFAPipeline


def _pdf(
    *,
    content: bytes = b"0 0 0 rg\n10 10 40 40 re f\n",
    resources: PDFDict | None = None,
    javascript: bool = False,
) -> bytes:
    builder = PDFBuilder(version="1.4")
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": resources or PDFDict(),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    catalog = PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref})
    if javascript:
        catalog["OpenAction"] = PDFDict(
            {"S": PDFName("JavaScript"), "JS": b"app.alert('x')"}
        )
    root = builder.add(catalog)
    builder.set_root(root)
    return builder.to_bytes()


def _transparent_resources() -> PDFDict:
    return PDFDict(
        {
            "ExtGState": PDFDict(
                {
                    "GS": PDFDict(
                        {"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5}
                    )
                }
            )
        }
    )


def test_auto_pipeline_uses_semantic_fidelity_for_structural_repair(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(fidelity="auto").convert(
        _pdf(javascript=True), output, level="2b"
    )
    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "semantic"
    assert result.fidelity is not None and result.fidelity.passed
    assert b"JavaScript" not in output.read_bytes()
    assert NativePDFAValidator().validate(output, "2b").compliant


def test_auto_pipeline_upgrades_to_visual_fidelity_for_pdfa1_flattening(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(
        _pdf(
            content=b"/GS gs\n1 0 0 rg\n10 10 60 60 re f\n",
            resources=_transparent_resources(),
        ),
        output,
        level="1b",
    )
    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert NativePDFAValidator().validate(output, "1b").compliant


def test_forced_semantic_mode_refuses_intentional_page_rewrite(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    output.write_bytes(b"sentinel")
    try:
        OwnedPDFAPipeline(
            fidelity="semantic",
            transparency_dpi=72,
        ).convert(
            _pdf(
                content=b"/GS gs\n1 0 0 rg\n10 10 60 60 re f\n",
                resources=_transparent_resources(),
            ),
            output,
            level="1b",
        )
    except OwnedFidelityError:
        pass
    else:
        raise AssertionError("semantic fidelity must reject a raster painting rewrite")
    assert output.read_bytes() == b"sentinel"


def test_visual_fidelity_can_be_required_for_non_flattening_conversion(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="visual",
        visual_dpi=72,
    ).convert(_pdf(javascript=True), output, level="2b")
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
