from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline
from pdf2pdfa.native.pdfa import NativePDFAValidator


def _transparent_axial_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    function = PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "C0": [1, 0, 0],
            "C1": [0, 0, 1],
            "N": 1,
        }
    )
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [0, 0, 100, 0],
            "Extend": [True, True],
            "Function": function,
        }
    )
    resources = PDFDict(
        {
            "Shading": PDFDict({"Sh": shading}),
            "ExtGState": PDFDict(
                {
                    "GS": PDFDict(
                        {"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5}
                    )
                }
            ),
        }
    )
    content = builder.add(PDFStream(PDFDict(), b"/GS gs /Sh sh\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 20],
                "Resources": resources,
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_shading_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_axial_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
