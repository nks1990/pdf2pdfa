from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


def _transparent_ccitt_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    image = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Image"),
                    "Width": 8,
                    "Height": 1,
                    "BitsPerComponent": 1,
                    "ColorSpace": PDFName("DeviceGray"),
                    "Filter": PDFName("CCITTFaxDecode"),
                    "DecodeParms": PDFDict({"K": 0, "Columns": 8, "Rows": 1}),
                }
            ),
            # white4 / black4
            _bits("1011 011"),
        )
    )
    resources = PDFDict(
        {
            "XObject": PDFDict({"Im": image}),
            "ExtGState": PDFDict(
                {
                    "GS": PDFDict(
                        {"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5}
                    )
                }
            ),
        }
    )
    content = builder.add(
        PDFStream(PDFDict(), b"/GS gs 80 0 0 20 0 0 cm /Im Do\n")
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 80, 20],
                "Resources": resources,
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_pdfa1_pipeline_flattens_transparent_ccitt_with_visual_fidelity(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_transparent_ccitt_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
