from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.repair_owned import OwnedRepairEngine, flatten_page_numbers


def _document(
    *,
    content: bytes,
    resources: PDFDict | None = None,
    page_extra: PDFDict | None = None,
) -> bytes:
    builder = PDFBuilder(version="1.4")
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = PDFDict(
        {
            "Type": PDFName("Page"),
            "Parent": pages_ref,
            "MediaBox": [0, 0, 200, 200],
            "CropBox": [0, 0, 200, 200],
            "Resources": resources or PDFDict(),
            "Contents": content_ref,
        }
    )
    if page_extra:
        page.update(page_extra)
    page_ref = builder.add(page)
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _transparent_resources() -> PDFDict:
    return PDFDict(
        {
            "ExtGState": PDFDict(
                {
                    "GS0": PDFDict(
                        {
                            "Type": PDFName("ExtGState"),
                            "ca": 0.5,
                            "CA": 0.5,
                        }
                    )
                }
            )
        }
    )


def test_owned_planner_maps_page_transparency_to_flattening():
    source = _document(
        content=b"/GS0 gs\n1 0 0 rg\n20 20 100 100 re f\n",
        resources=_transparent_resources(),
    )
    plan = OwnedRepairEngine(transparency_dpi=72).plan(source, "1b")
    assert plan.repairable, plan.blockers
    assert flatten_page_numbers(plan) == (1,)
    assert any("flatten PDF/A-1 transparency" in operation for operation in plan.operations)


def test_owned_repair_flattens_pdfa1_transparency(tmp_path: Path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "archive.pdf"
    source.write_bytes(
        _document(
            content=b"/GS0 gs\n1 0 0 rg\n20 20 100 100 re f\n",
            resources=_transparent_resources(),
        )
    )

    result = OwnedRepairEngine(transparency_dpi=72).convert(source, destination, "1b")

    assert result.validation.compliant, result.validation.failures
    assert NativePDFAValidator().validate(destination, "1b").compliant
    assert any("flattened 1 transparent page" in operation for operation in result.plan.operations)
    data = destination.read_bytes()
    assert b"PDF2PDFAFlatten1" in data
    assert b"/SMask" not in data


def test_annotation_appearance_transparency_remains_fail_closed():
    appearance = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Form"),
                "BBox": [0, 0, 20, 20],
                "Resources": _transparent_resources(),
            }
        ),
        b"/GS0 gs\n1 0 0 rg\n0 0 20 20 re f\n",
    )
    annotation = PDFDict(
        {
            "Type": PDFName("Annot"),
            "Subtype": PDFName("Text"),
            "Rect": [10, 10, 30, 30],
            "F": 4,
            "AP": PDFDict({"N": appearance}),
        }
    )
    source = _document(content=b"q Q\n", page_extra=PDFDict({"Annots": [annotation]}))

    plan = OwnedRepairEngine(transparency_dpi=72).plan(source, "1b")

    assert not plan.repairable
    assert any(blocker.code == "pdfa1.annotation_transparency" for blocker in plan.blockers)
