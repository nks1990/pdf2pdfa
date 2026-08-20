from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_cff_pdf_render import _simple_cff, _simple_font
from tests.native.test_uncolored_pattern_render import _pattern


def _finish(builder: PDFBuilder, pattern: PDFStream, content: bytes, extra: PDFDict | None = None) -> bytes:
    pattern_ref = builder.add(pattern)
    resources = PDFDict(
        {
            "Pattern": PDFDict({"P": pattern_ref}),
            "ColorSpace": PDFDict(
                {"UC": [PDFName("Pattern"), PDFName("DeviceRGB")]}
            ),
        }
    )
    if extra:
        resources.update(extra)
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 40, 20],
                "Resources": resources,
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y: int = 5):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_uncolored_pattern_cff_text_contributes_shape_not_intrinsic_color():
    font = _simple_font(_simple_cff(b"A"), encoding=PDFName("WinAnsiEncoding"))
    pattern = _pattern(
        content=b"BT /F 6 Tf 1 0 0 1 0 0 Tm <41> Tj ET\n",
        resources=PDFDict({"Font": PDFDict({"F": font})}),
    )
    builder = PDFBuilder(version="1.7")
    pdf = _finish(
        builder,
        pattern,
        b"/UC cs 0 1 0 /P scn 0 0 40 20 re f\n",
    )
    page = render_page_full(pdf, dpi=72)
    painted = [
        _pixel(page, x, y)
        for y in range(1, 10)
        for x in range(0, 10)
    ]
    assert any(pixel.g > 0.9 and pixel.r < 0.1 and pixel.b < 0.1 for pixel in painted)
    assert not any(pixel.r < 0.1 and pixel.g < 0.1 and pixel.b < 0.1 for pixel in painted)


def test_outer_luminosity_soft_mask_is_applied_once_to_uncolored_pattern():
    builder = PDFBuilder(version="1.7")
    mask_form = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 40, 20],
                    "Resources": PDFDict(),
                    "Group": PDFDict(
                        {
                            "S": PDFName("Transparency"),
                            "I": True,
                            "K": False,
                        }
                    ),
                }
            ),
            b"0.5 g 0 0 40 20 re f\n",
        )
    )
    smask = PDFDict(
        {
            "S": PDFName("Luminosity"),
            "G": mask_form,
            "BC": [0],
        }
    )
    pattern = _pattern(content=b"0 0 10 10 re f\n")
    pdf = _finish(
        builder,
        pattern,
        b"/Mask gs /UC cs 1 0 0 /P scn 0 0 40 20 re f\n",
        PDFDict({"ExtGState": PDFDict({"Mask": PDFDict({"SMask": smask})})}),
    )
    page = render_page_full(pdf, dpi=72)
    pixel = _pixel(page, 5)
    assert pixel.r > 0.98
    # Once: 50% red over white -> G/B about 0.5. Twice would be about 0.75.
    assert 0.45 < pixel.g < 0.55
    assert 0.45 < pixel.b < 0.55
