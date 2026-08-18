from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError

from tests.native.test_annotation_render import _appearance


def _stateful_pdf(normal: PDFDict, *, as_value=None) -> bytes:
    builder = PDFBuilder(version="1.7")
    mapped = PDFDict()
    for name, value in normal.items():
        mapped[name] = builder.add(value) if isinstance(value, PDFStream) else value
    annot = PDFDict(
        {
            "Type": PDFName("Annot"),
            "Subtype": PDFName("Stamp"),
            "Rect": [10, 10, 30, 30],
            "F": 4,
            "AP": PDFDict({"N": mapped}),
        }
    )
    if as_value is not None:
        annot["AS"] = as_value
    annot_ref = builder.add(annot)
    content = builder.add(PDFStream(PDFDict(), b""))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 50, 50],
                "Resources": PDFDict(),
                "Contents": content,
                "Annots": [annot_ref],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_stateful_appearance_rejects_unknown_as_name():
    normal = {
        "On": _appearance(b"1 0 0 rg 0 0 10 10 re f\n"),
        "Off": _appearance(b"0 1 0 rg 0 0 10 10 re f\n"),
    }
    with pytest.raises(RenderingError, match="does not select"):
        render_page_full(
            _stateful_pdf(normal, as_value=PDFName("Missing")),
            dpi=72,
        )


def test_stateful_appearance_rejects_non_name_as():
    normal = {"On": _appearance(b"1 0 0 rg 0 0 10 10 re f\n")}
    with pytest.raises(RenderingError, match="/AS shall be a name"):
        render_page_full(_stateful_pdf(normal, as_value=b"On"), dpi=72)


def test_stateful_appearance_rejects_non_stream_state_value():
    normal = {
        "On": _appearance(b"1 0 0 rg 0 0 10 10 re f\n"),
        "Broken": PDFDict({"Not": PDFName("AStream")}),
    }
    with pytest.raises(RenderingError, match="state /Broken is not an appearance stream"):
        render_page_full(_stateful_pdf(normal, as_value=PDFName("On")), dpi=72)
