from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline

from tests.native.test_uncolored_pattern_render import _pattern


def _transparent_uncolored_pattern_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    pattern_ref = builder.add(_pattern(content=b"0 0 5 10 re f\n"))
    resources = PDFDict(
        {
            "Pattern": PDFDict({"P": pattern_ref}),
            "ColorSpace": PDFDict(
                {"UC": [PDFName("Pattern"), PDFName("DeviceRGB")]}
            ),
            "ExtGState": PDFDict(
                {
                    "Half": PDFDict(
                        {"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5}
                    )
                }
            ),
        }
    )
    content = builder.add(
        PDFStream(
            PDFDict(),
            b"/Half gs /UC cs 1 0 0 /P scn 0 0 40 20 re f\n",
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 40, 20],
                "Resources": resources,
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_uncolored_tiling_pattern(tmp_path: Path):
    output = tmp_path / "uncolored-pattern.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_uncolored_pattern_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
