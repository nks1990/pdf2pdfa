from __future__ import annotations

from decimal import Decimal

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import UnsupportedRenderingError


def _knockout_pdf(*, isolated: bool, alpha_is_shape: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    half = PDFDict(
        {
            "Type": PDFName("ExtGState"),
            "ca": Decimal("0.5"),
            "AIS": alpha_is_shape,
        }
    )
    form = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 100, 100],
                    "Resources": PDFDict({"ExtGState": PDFDict({"Half": half})}),
                    "Group": PDFDict(
                        {
                            "S": PDFName("Transparency"),
                            "I": isolated,
                            "K": True,
                        }
                    ),
                }
            ),
            (
                b"/Half gs 1 0 0 rg 10 10 50 50 re f\n"
                b"/Half gs 0 1 0 rg 30 30 50 50 re f\n"
            ),
        )
    )
    contents = builder.add(
        PDFStream(
            PDFDict(),
            b"0 0 1 rg 0 0 100 100 re f\n/F Do\n",
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
                "Resources": PDFDict({"XObject": PDFDict({"F": form})}),
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y_from_bottom: int):
    return page.surface.get_pixel(x, page.height - 1 - y_from_bottom)


@pytest.mark.parametrize("isolated", [True, False])
def test_knockout_second_half_opaque_object_replaces_first_over_full_shape(isolated: bool):
    page = render_page_full(_knockout_pdf(isolated=isolated), dpi=72)

    red_only = _pixel(page, 20, 20)
    overlap = _pixel(page, 40, 40)
    green_only = _pixel(page, 70, 70)

    # Half-opaque red/green over the page's opaque blue backdrop.
    assert red_only.r == pytest.approx(0.5, abs=2 / 255)
    assert red_only.g < 0.02
    assert red_only.b == pytest.approx(0.5, abs=2 / 255)

    # Knockout is the key assertion: the second green object removes the red
    # sibling throughout its full shape before its 0.5 opacity is composited.
    assert overlap.r < 0.02
    assert overlap.g == pytest.approx(0.5, abs=2 / 255)
    assert overlap.b == pytest.approx(0.5, abs=2 / 255)

    assert green_only.r < 0.02
    assert green_only.g == pytest.approx(0.5, abs=2 / 255)
    assert green_only.b == pytest.approx(0.5, abs=2 / 255)


def test_knockout_ais_true_remains_explicitly_fail_closed():
    with pytest.raises(UnsupportedRenderingError, match="AIS true"):
        render_page_full(
            _knockout_pdf(isolated=True, alpha_is_shape=True),
            dpi=72,
        )
