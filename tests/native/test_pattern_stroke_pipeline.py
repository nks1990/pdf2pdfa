from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline


def _transparent_pattern_stroke_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [0, 0, 100, 0],
            "Extend": [True, True],
            "Function": PDFDict(
                {
                    "FunctionType": 2,
                    "Domain": [0, 1],
                    "C0": [1, 0, 0],
                    "C1": [0, 0, 1],
                    "N": 1,
                }
            ),
        }
    )
    pattern_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Pattern"),
                "PatternType": 2,
                "Shading": shading,
            }
        )
    )
    resources = PDFDict(
        {
            "Pattern": PDFDict({"P": pattern_ref}),
            "ExtGState": PDFDict(
                {
                    "GS": PDFDict(
                        {
                            "Type": PDFName("ExtGState"),
                            "CA": 0.5,
                            "ca": 1.0,
                        }
                    )
                }
            ),
        }
    )
    contents = builder.add(
        PDFStream(
            PDFDict(),
            b"/GS gs 12 w /Pattern CS /P SCN 10 20 m 90 20 l S\n",
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 40],
                "Resources": resources,
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_pattern_stroke_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "pattern-stroke-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_pattern_stroke_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
