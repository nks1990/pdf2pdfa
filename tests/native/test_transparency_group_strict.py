from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError


def _pdf(group: PDFDict, *, two_calls: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    form = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 20, 20],
                    "Resources": PDFDict(
                        {
                            "ExtGState": PDFDict(
                                {
                                    "Half": PDFDict(
                                        {"Type": PDFName("ExtGState"), "ca": 0.5}
                                    )
                                }
                            )
                        }
                    ),
                    "Group": group,
                }
            ),
            b"/Half gs 1 0 0 rg 0 0 20 20 re f\n",
        )
    )
    program = b"0 0 1 rg 0 0 50 20 re f /Fm Do\n"
    if two_calls:
        # The second invocation is translated into the right half. Both halves
        # have identical blue backdrops, so any state leak between executions
        # becomes a visible left/right mismatch.
        program = (
            b"0 0 1 rg 0 0 50 20 re f "
            b"q /Fm Do Q q 1 0 0 1 25 0 cm /Fm Do Q\n"
        )
    content = builder.add(PDFStream(PDFDict(), program))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 50, 20],
                "Resources": PDFDict({"XObject": PDFDict({"Fm": form})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y: int = 10):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_group_isolated_flag_must_be_pdf_boolean():
    group = PDFDict({"S": PDFName("Transparency"), "I": 1, "K": False})
    with pytest.raises(RenderingError, match=r"Group /I shall be boolean"):
        render_page_full(_pdf(group), dpi=72)


def test_group_knockout_flag_must_be_pdf_boolean():
    group = PDFDict({"S": PDFName("Transparency"), "I": False, "K": PDFName("False")})
    with pytest.raises(RenderingError, match=r"Group /K shall be boolean"):
        render_page_full(_pdf(group), dpi=72)


def test_repeated_nonisolated_form_invocations_are_state_deterministic():
    group = PDFDict({"S": PDFName("Transparency"), "I": False, "K": False})
    page = render_page_full(_pdf(group, two_calls=True), dpi=72)
    left = _pixel(page, 10)
    right = _pixel(page, 35)
    assert abs(left.r - right.r) < 0.01
    assert abs(left.g - right.g) < 0.01
    assert abs(left.b - right.b) < 0.01
    assert abs(left.a - right.a) < 0.01
