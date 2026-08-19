from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import UnsupportedRenderingError


def _pdf_with_knockout_form(content: bytes, extgstates: PDFDict) -> bytes:
    builder = PDFBuilder(version="1.7")
    form = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 40, 40],
                    "Resources": PDFDict({"ExtGState": extgstates}),
                    "Group": PDFDict(
                        {
                            "S": PDFName("Transparency"),
                            "I": True,
                            "K": True,
                        }
                    ),
                }
            ),
            content,
        )
    )
    stream = builder.add(PDFStream(PDFDict(), b"/F Do"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 40, 40],
                "Resources": PDFDict({"XObject": PDFDict({"F": form})}),
                "Contents": stream,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_soft_mask_set_inside_knockout_group_is_fail_closed_before_mask_execution():
    states = PDFDict(
        {
            "Masked": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    # A dictionary is enough to exercise the knockout boundary:
                    # the guard must reject it before attempting to build SMask/G.
                    "SMask": PDFDict({"S": PDFName("Alpha")}),
                }
            )
        }
    )
    with pytest.raises(UnsupportedRenderingError, match="soft masks inside a knockout group"):
        render_page_full(
            _pdf_with_knockout_form(b"/Masked gs 0 0 20 20 re f", states),
            dpi=72,
        )


def test_text_knockout_false_is_fail_closed_at_text_show_boundary():
    states = PDFDict(
        {
            "NoTK": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    "TK": False,
                }
            )
        }
    )
    # No font is deliberately installed. The TK=false transaction guard must
    # fire at Tj before font decoding can become a competing failure mode.
    with pytest.raises(UnsupportedRenderingError, match="TK false"):
        render_page_full(
            _pdf_with_knockout_form(b"/NoTK gs BT (A) Tj ET", states),
            dpi=72,
        )


def test_unknown_non_boolean_tk_is_rejected_as_malformed_not_coerced():
    states = PDFDict(
        {
            "Bad": PDFDict(
                {
                    "Type": PDFName("ExtGState"),
                    "TK": 1,
                }
            )
        }
    )
    with pytest.raises(Exception, match="ExtGState/TK expects a boolean"):
        render_page_full(
            _pdf_with_knockout_form(b"/Bad gs 0 0 20 20 re f", states),
            dpi=72,
        )
