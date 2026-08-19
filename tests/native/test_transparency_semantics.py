from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer
from pdf2pdfa.native.page_render import RenderingError
from pdf2pdfa.native.structure import walk_pages


def _pdf(content: bytes, states: PDFDict) -> bytes:
    builder = PDFBuilder(version="1.7")
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 20, 20],
                "Resources": PDFDict({"ExtGState": states}),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _render_and_return_renderer(source: bytes) -> FullOwnedPageRenderer:
    doc = PDFDocument.open(source, repair=True)
    renderer = FullOwnedPageRenderer(doc, dpi=72)
    renderer.render_page(next(iter(walk_pages(doc))))
    return renderer


def test_ais_and_tk_have_pdf_defaults():
    renderer = _render_and_return_renderer(_pdf(b"", PDFDict()))
    assert renderer.alpha_is_shape is False
    assert renderer.text_knockout is True


def test_extgstate_tracks_ais_and_tk():
    states = PDFDict(
        {
            "G": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    "AIS": True,
                    "TK": False,
                }
            )
        }
    )
    renderer = _render_and_return_renderer(_pdf(b"/G gs", states))
    assert renderer.alpha_is_shape is True
    assert renderer.text_knockout is False


def test_q_q_restores_ais_and_tk():
    states = PDFDict(
        {
            "Outer": PDFDict({"Type": PDFName("ExtGState"), "AIS": False, "TK": True}),
            "Inner": PDFDict({"Type": PDFName("ExtGState"), "AIS": True, "TK": False}),
        }
    )
    renderer = _render_and_return_renderer(
        _pdf(b"/Outer gs q /Inner gs Q", states)
    )
    assert renderer.alpha_is_shape is False
    assert renderer.text_knockout is True


@pytest.mark.parametrize("key", ["AIS", "TK"])
def test_ais_and_tk_require_real_pdf_booleans(key: str):
    states = PDFDict(
        {
            "Bad": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    key: 1,
                }
            )
        }
    )
    with pytest.raises(RenderingError, match=f"ExtGState/{key} expects a boolean"):
        _render_and_return_renderer(_pdf(b"/Bad gs", states))
