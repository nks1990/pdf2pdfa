from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.pipeline import OwnedPDFAPipeline
from pdf2pdfa.native.repair_owned import (
    OwnedRepairEngine,
    flatten_annotation_page_numbers,
    flatten_page_numbers,
)
from pdf2pdfa.native.structure import resolve, walk_pages


def _source(*, page_alpha: float | None = None, stateful: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    ap_gs = PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})

    def appearance(color: bytes) -> PDFStream:
        return PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 10, 10],
                    "Resources": PDFDict(
                        {"ExtGState": PDFDict({"Half": ap_gs})}
                    ),
                }
            ),
            b"/Half gs " + color + b" 0 0 10 10 re f\n",
        )

    if stateful:
        normal = PDFDict(
            {
                "On": builder.add(appearance(b"1 0 0 rg")),
                "Off": builder.add(appearance(b"0 1 0 rg")),
            }
        )
        rollover = builder.add(appearance(b"0 0 1 rg"))
        ap = PDFDict({"N": normal, "R": rollover})
    else:
        ap = PDFDict({"N": builder.add(appearance(b"1 0 0 rg"))})

    annot = PDFDict(
        {
            "Type": PDFName("Annot"),
            "Subtype": PDFName("Stamp"),
            "Rect": [20, 20, 80, 80],
            "F": 4,
            "AP": ap,
        }
    )
    if stateful:
        annot["AS"] = PDFName("On")
    annot_ref = builder.add(annot)

    resources = PDFDict()
    if page_alpha is not None:
        resources["ExtGState"] = PDFDict(
            {"PageAlpha": PDFDict({"Type": PDFName("ExtGState"), "ca": page_alpha})}
        )
        prefix = b"/PageAlpha gs "
    else:
        prefix = b""
    content_ref = builder.add(
        PDFStream(
            PDFDict(),
            prefix + b"0 0 1 rg 0 0 100 100 re f\n",
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
                "Contents": content_ref,
                "Annots": [annot_ref],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _normal_stream(doc: PDFDocument):
    page = next(walk_pages(doc))
    annots = resolve(doc, page.dictionary.get("Annots"))
    assert isinstance(annots, list) and annots
    annot = resolve(doc, annots[0])
    assert isinstance(annot, PDFDict)
    ap = resolve(doc, annot.get("AP"))
    assert isinstance(ap, PDFDict)
    normal = resolve(doc, ap.get("N"))
    if isinstance(normal, PDFStream):
        return annot, normal
    assert isinstance(normal, PDFDict)
    selected = resolve(doc, normal["On"])
    assert isinstance(selected, PDFStream)
    return annot, selected


def test_annotation_transparency_is_baked_and_ap_is_neutralized(tmp_path: Path):
    source = _source()
    output = tmp_path / "annotation-a1.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto",
        transparency_dpi=72,
        visual_dpi=72,
        visual_pixel_tolerance=2,
        visual_max_mean_error=1.0,
        visual_max_changed_pixel_ratio=0.01,
    ).convert(source, output, level="1b")

    assert result.validation.compliant, result.validation.failures
    assert result.fidelity_mode == "visual"
    assert result.fidelity is not None and result.fidelity.passed
    assert flatten_annotation_page_numbers(result.plan) == (1,)
    assert flatten_page_numbers(result.plan) == ()
    assert any("neutralized 1 appearance stream" in item for item in result.plan.operations)

    candidate = PDFDocument.open(output.read_bytes(), repair=False)
    annot, normal = _normal_stream(candidate)
    assert normal.data == b""
    assert resolve(candidate, normal.get("Resources")) == PDFDict()
    assert resolve(candidate, annot.get("Subtype")) == PDFName("Stamp")
    assert resolve(candidate, annot.get("Rect")) == [20, 20, 80, 80]
    assert NativePDFAValidator().validate(candidate, "1b").compliant

    rendered = render_page_full(candidate, dpi=72)
    center = rendered.surface.get_pixel(50, rendered.height - 1 - 50)
    outside = rendered.surface.get_pixel(10, rendered.height - 1 - 10)
    assert 0.45 < center.r < 0.55 and center.g < 0.05 and 0.45 < center.b < 0.55
    assert outside.r < 0.05 and outside.g < 0.05 and outside.b > 0.95


def test_stateful_and_rollover_appearances_are_neutralized_but_as_is_preserved(tmp_path: Path):
    output = tmp_path / "stateful-a1.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto", transparency_dpi=72, visual_dpi=72
    ).convert(_source(stateful=True), output, level="1b")
    assert result.validation.compliant
    assert any("neutralized 3 appearance stream" in item for item in result.plan.operations)

    doc = PDFDocument.open(output.read_bytes(), repair=False)
    page = next(walk_pages(doc))
    annots = resolve(doc, page.dictionary.get("Annots"))
    annot = resolve(doc, annots[0])  # type: ignore[index]
    assert isinstance(annot, PDFDict)
    assert resolve(doc, annot.get("AS")) == PDFName("On")
    ap = resolve(doc, annot.get("AP"))
    assert isinstance(ap, PDFDict)
    normal = resolve(doc, ap.get("N"))
    assert isinstance(normal, PDFDict)
    for state in ("On", "Off"):
        stream = resolve(doc, normal[state])
        assert isinstance(stream, PDFStream) and stream.data == b""
    rollover = resolve(doc, ap.get("R"))
    assert isinstance(rollover, PDFStream) and rollover.data == b""


def test_annotation_page_supersedes_page_content_flatten_when_both_are_transparent():
    source = _source(page_alpha=0.8)
    plan = OwnedRepairEngine(transparency_dpi=72).plan(source, "1b")
    assert not plan.blockers, plan.blockers
    assert flatten_annotation_page_numbers(plan) == (1,)
    assert flatten_page_numbers(plan) == ()


def test_ordinary_page_transparency_keeps_opaque_annotation_as_live_ap(tmp_path: Path):
    # Build a source with transparent page content but an opaque annotation.
    builder = PDFBuilder(version="1.7")
    ap = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 10, 10],
                    "Resources": PDFDict(),
                }
            ),
            b"1 0 0 rg 0 0 10 10 re f\n",
        )
    )
    annot = builder.add(
        PDFDict(
            {
                "Type": PDFName("Annot"),
                "Subtype": PDFName("Stamp"),
                "Rect": [20, 20, 80, 80],
                "F": 4,
                "AP": PDFDict({"N": ap}),
            }
        )
    )
    content = builder.add(
        PDFStream(PDFDict(), b"/Half gs 0 0 1 rg 0 0 100 100 re f\n")
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(
                    {"ExtGState": PDFDict({"Half": PDFDict({"ca": 0.5})})}
                ),
                "Contents": content,
                "Annots": [annot],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)

    output = tmp_path / "page-only-a1.pdf"
    result = OwnedPDFAPipeline(
        fidelity="auto", transparency_dpi=72, visual_dpi=72
    ).convert(builder.to_bytes(), output, level="1b")
    assert result.validation.compliant
    assert flatten_page_numbers(result.plan) == (1,)
    assert flatten_annotation_page_numbers(result.plan) == ()

    doc = PDFDocument.open(output.read_bytes(), repair=False)
    _annot, normal = _normal_stream(doc)
    assert normal.data != b""  # annotation remains live; it was not baked into page raster
