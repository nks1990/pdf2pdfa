from __future__ import annotations

from decimal import Decimal

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_patch_shading67 import BOUNDARY12, INTERIOR4, _record


def _green_patch(shading_type: int) -> PDFStream:
    points = list(BOUNDARY12) + (INTERIOR4 if shading_type == 7 else [])
    data = _record(0, points, [(0, 255, 0)] * 4)
    return PDFStream(
        PDFDict(
            {
                "ShadingType": shading_type,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerCoordinate": 8,
                "BitsPerComponent": 8,
                "BitsPerFlag": 8,
                "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
                "BBox": [0, 0, 100, 100],
            }
        ),
        data,
    )


def _pdf(shading_type: int, *, isolated: bool) -> bytes:
    builder = PDFBuilder(version="1.7")
    shading = builder.add(_green_patch(shading_type))
    half = PDFDict(
        {
            "Type": PDFName("ExtGState"),
            "ca": Decimal("0.5"),
        }
    )
    form = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 100, 100],
                    "Resources": PDFDict(
                        {
                            "ExtGState": PDFDict({"Half": half}),
                            "Shading": PDFDict({"Sh": shading}),
                        }
                    ),
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
                b"/Half gs 1 0 0 rg 0 0 100 100 re f\n"
                b"/Half gs /Sh sh\n"
            ),
        )
    )
    contents = builder.add(
        PDFStream(PDFDict(), b"0 0 1 rg 0 0 100 100 re f /F Do")
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


@pytest.mark.parametrize("shading_type", [6, 7])
@pytest.mark.parametrize("isolated", [True, False])
def test_patch_shading_is_one_knockout_object_with_alpha_as_opacity(
    shading_type: int,
    isolated: bool,
):
    page = render_page_full(_pdf(shading_type, isolated=isolated), dpi=72)
    pixel = page.surface.get_pixel(50, 49)

    # The green shading is the second knockout sibling. It must remove the
    # earlier red sibling over its full shape, while ca=.5 remains opacity.
    assert pixel.r < 0.03
    assert pixel.g == pytest.approx(0.5, abs=3 / 255)
    assert pixel.b == pytest.approx(0.5, abs=3 / 255)
