from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline


def _transparent_function_shading_pdf() -> bytes:
    function = PDFStream(
        PDFDict(
            {
                "FunctionType": 4,
                "Domain": [0, 1, 0, 1],
                "Range": [0, 1, 0, 1, 0, 1],
            }
        ),
        b"0.5",
    )
    shading = PDFDict(
        {
            "ShadingType": 1,
            "ColorSpace": PDFName("DeviceRGB"),
            "Domain": [0, 1, 0, 1],
            "Matrix": [100, 0, 0, 100, 0, 0],
            "Function": function,
        }
    )
    builder = PDFBuilder(version="1.7")
    shading_ref = builder.add(shading)
    resources = PDFDict(
        {
            "Shading": PDFDict({"Sh": shading_ref}),
            "ExtGState": PDFDict(
                {"GS": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5})}
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
                "MediaBox": [0, 0, 100, 100],
                "Resources": resources,
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_function_shading(tmp_path: Path):
    output = tmp_path / "function-shading.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_function_shading_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
