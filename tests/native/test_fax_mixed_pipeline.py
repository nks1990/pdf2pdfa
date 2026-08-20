from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline


EOL = "000000000001"


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


def _mixed_transparent_pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    encoded = _bits(
        f"{EOL} 1 1011 011 "  # row 1: white4 black4 (1D reference)
        f"{EOL} 0 1 1"        # row 2: identical via two V0 modes
    )
    image = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Image"),
                    "Width": 8,
                    "Height": 2,
                    "BitsPerComponent": 1,
                    "ColorSpace": PDFName("DeviceGray"),
                    "Filter": PDFName("CCITTFaxDecode"),
                    "DecodeParms": PDFDict(
                        {
                            "K": 2,
                            "Columns": 8,
                            "Rows": 2,
                            "EndOfLine": True,
                        }
                    ),
                }
            ),
            encoded,
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


def test_mixed_group3_survives_owned_pdfa1_flatten_and_visual_gate(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
    ).convert(_mixed_transparent_pdf(), output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert any("flattened 1 transparent page" in item for item in result.plan.operations)
    assert NativePDFAValidator().validate(output, "1b").compliant
