from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline

from tests.native.test_cff_pdf_render import _cid_cff, _cid_font


def _transparent_fd_matrix_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(_cid_font(_cid_cff(fd_matrix=True)))
    resources = PDFDict(
        {
            "Font": PDFDict({"F": font_ref}),
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
            ),
        }
    )
    content = builder.add(
        PDFStream(
            PDFDict(),
            b"/GS gs BT /F 60 Tf 1 0 0 1 10 10 Tm <0064> Tj ET\n",
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
                "Resources": resources,
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_cff_fd_matrix_text_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "cff-fd-matrix-archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_fd_matrix_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
