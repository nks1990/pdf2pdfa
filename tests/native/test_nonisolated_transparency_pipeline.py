from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline


def _nonisolated_pdf() -> bytes:
    group_form = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Form"),
                "BBox": [0, 0, 100, 100],
                "Resources": PDFDict(
                    {
                        "ExtGState": PDFDict(
                            {
                                "MH": PDFDict(
                                    {
                                        "Type": PDFName("ExtGState"),
                                        "ca": 0.5,
                                        "BM": PDFName("Multiply"),
                                    }
                                )
                            }
                        )
                    }
                ),
                "Group": PDFDict(
                    {
                        "S": PDFName("Transparency"),
                        "I": False,
                        "K": False,
                    }
                ),
            }
        ),
        b"/MH gs 1 0 0 rg 0 0 100 100 re f\n",
    )
    builder = PDFBuilder(version="1.7")
    form_ref = builder.add(group_form)
    content = builder.add(
        PDFStream(
            PDFDict(),
            b"0 0 1 rg 0 0 100 100 re f /Fm Do\n",
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"XObject": PDFDict({"Fm": form_ref})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_nonisolated_transparency_group(tmp_path: Path):
    output = tmp_path / "nonisolated.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_nonisolated_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
