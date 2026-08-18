from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.repair_owned import OwnedRepairEngine
from pdf2pdfa.native.visual_fidelity import NativeVisualFidelityChecker


def _pdf(content: bytes, *, resources: PDFDict | None = None, rotate: int = 0) -> bytes:
    builder = PDFBuilder(version="1.4")
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = PDFDict(
        {
            "Type": PDFName("Page"),
            "Parent": pages_ref,
            "MediaBox": [0, 0, 120, 80],
            "CropBox": [0, 0, 120, 80],
            "Resources": resources or PDFDict(),
            "Contents": contents,
        }
    )
    if rotate:
        page["Rotate"] = rotate
    page_ref = builder.add(page)
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _transparency() -> PDFDict:
    return PDFDict(
        {
            "ExtGState": PDFDict(
                {"GS": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5})}
            )
        }
    )


def test_identical_documents_have_zero_visual_error():
    data = _pdf(b"0 0 0 rg\n10 10 40 30 re f\n")
    report = NativeVisualFidelityChecker(dpi=72).compare(data, data)
    assert report.passed
    assert len(report.pages) == 1
    assert report.pages[0].mean_channel_error == 0
    assert report.pages[0].changed_pixel_ratio == 0
    assert report.pages[0].max_channel_error == 0


def test_visual_gate_detects_paint_change():
    black = _pdf(b"0 0 0 rg\n10 10 40 30 re f\n")
    red = _pdf(b"1 0 0 rg\n10 10 40 30 re f\n")
    report = NativeVisualFidelityChecker(
        dpi=72,
        pixel_tolerance=0,
        max_mean_error=0,
        max_changed_pixel_ratio=0,
    ).compare(black, red)
    assert not report.passed
    assert report.pages[0].changed_pixel_ratio > 0
    assert report.pages[0].max_channel_error > 0


def test_visual_gate_respects_page_rotation():
    data = _pdf(b"0 0 1 rg\n10 10 40 30 re f\n", rotate=90)
    report = NativeVisualFidelityChecker(dpi=72).compare(data, data)
    assert report.passed
    assert report.pages[0].width == 80
    assert report.pages[0].height == 120


def test_pdfa1_native_flattening_preserves_rendered_appearance(tmp_path: Path):
    source = tmp_path / "transparent.pdf"
    candidate = tmp_path / "archive.pdf"
    source.write_bytes(
        _pdf(
            b"/GS gs\n1 0 0 rg\n10 10 70 50 re f\n",
            resources=_transparency(),
        )
    )

    OwnedRepairEngine(transparency_dpi=72).convert(source, candidate, "1b")
    report = NativeVisualFidelityChecker(
        dpi=72,
        pixel_tolerance=2,
        max_mean_error=1.0,
        max_changed_pixel_ratio=0.01,
    ).compare(source, candidate)

    assert report.passed, report.differences
