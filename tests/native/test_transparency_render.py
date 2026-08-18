from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.transparency_render import (
    UnsupportedRenderingError,
    render_page,
)


def _add_form(
    builder: PDFBuilder,
    content: bytes,
    *,
    isolated: bool = True,
    knockout: bool = False,
    bbox=(0, 0, 100, 100),
):
    group = PDFDict(
        {
            "S": PDFName("Transparency"),
            "I": isolated,
            "K": knockout,
        }
    )
    return builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": list(bbox),
                    "Resources": PDFDict(),
                    "Group": group,
                }
            ),
            content,
        )
    )


def _finish(builder: PDFBuilder, content: bytes, resources: PDFDict) -> bytes:
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "CropBox": [0, 0, 100, 100],
                "Resources": resources,
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y_pdf: int):
    return page.surface.get_pixel(x, 100 - y_pdf)


def test_isolated_group_applies_caller_alpha_once_not_twice():
    builder = PDFBuilder(version="1.7")
    form = _add_form(builder, b"1 0 0 rg 0 0 100 100 re f\n")
    resources = PDFDict(
        {
            "XObject": PDFDict({"Fm": form}),
            "ExtGState": PDFDict({"Half": PDFDict({"ca": 0.5})}),
        }
    )
    pdf = _finish(builder, b"/Half gs /Fm Do\n", resources)
    page = render_page(pdf, dpi=72)
    pixel = _pixel(page, 50, 50)
    assert pixel.r > 0.99
    assert 0.48 < pixel.g < 0.52
    assert 0.48 < pixel.b < 0.52


def test_isolated_group_boundary_blend_mode_is_applied_to_group_result():
    builder = PDFBuilder(version="1.7")
    form = _add_form(builder, b"1 0 0 rg 0 0 100 100 re f\n")
    resources = PDFDict(
        {
            "XObject": PDFDict({"Fm": form}),
            "ExtGState": PDFDict({"Mul": PDFDict({"BM": PDFName("Multiply")})}),
        }
    )
    pdf = _finish(
        builder,
        b"0 0 1 rg 0 0 100 100 re f /Mul gs /Fm Do\n",
        resources,
    )
    page = render_page(pdf, dpi=72)
    pixel = _pixel(page, 50, 50)
    assert pixel.r < 0.02 and pixel.g < 0.02 and pixel.b < 0.02


def test_alpha_soft_mask_limits_paint_to_mask_shape():
    builder = PDFBuilder(version="1.7")
    mask_group = _add_form(
        builder,
        b"0 0 0 rg 0 0 50 100 re f\n",
        isolated=True,
    )
    mask = PDFDict({"S": PDFName("Alpha"), "G": mask_group})
    resources = PDFDict(
        {
            "ExtGState": PDFDict({"Mask": PDFDict({"SMask": mask})}),
        }
    )
    pdf = _finish(
        builder,
        b"/Mask gs 1 0 0 rg 0 0 100 100 re f\n",
        resources,
    )
    page = render_page(pdf, dpi=72)
    left = _pixel(page, 25, 50)
    right = _pixel(page, 75, 50)
    assert left.r > 0.99 and left.g < 0.02 and left.b < 0.02
    assert right.r > 0.99 and right.g > 0.99 and right.b > 0.99


def test_luminosity_soft_mask_uses_group_luminosity():
    builder = PDFBuilder(version="1.7")
    mask_group = _add_form(
        builder,
        b"0.5 g 0 0 100 100 re f\n",
        isolated=True,
    )
    mask = PDFDict(
        {
            "S": PDFName("Luminosity"),
            "G": mask_group,
            "BC": [0],
        }
    )
    resources = PDFDict(
        {
            "ExtGState": PDFDict({"Mask": PDFDict({"SMask": mask})}),
        }
    )
    pdf = _finish(
        builder,
        b"/Mask gs 1 0 0 rg 0 0 100 100 re f\n",
        resources,
    )
    page = render_page(pdf, dpi=72)
    pixel = _pixel(page, 50, 50)
    assert pixel.r > 0.99
    assert 0.48 < pixel.g < 0.52
    assert 0.48 < pixel.b < 0.52


def test_soft_mask_transfer_function_is_applied():
    builder = PDFBuilder(version="1.7")
    mask_group = _add_form(
        builder,
        b"0.25 g 0 0 100 100 re f\n",
        isolated=True,
    )
    transfer = PDFDict(
        {
            "FunctionType": 2,
            "Domain": [0, 1],
            "Range": [0, 1],
            "C0": [1],
            "C1": [0],
            "N": 1,
        }
    )
    mask = PDFDict(
        {
            "S": PDFName("Luminosity"),
            "G": mask_group,
            "BC": [0],
            "TR": transfer,
        }
    )
    resources = PDFDict(
        {
            "ExtGState": PDFDict({"Mask": PDFDict({"SMask": mask})}),
        }
    )
    pdf = _finish(
        builder,
        b"/Mask gs 1 0 0 rg 0 0 100 100 re f\n",
        resources,
    )
    page = render_page(pdf, dpi=72)
    pixel = _pixel(page, 50, 50)
    # 25% gray -> TR(t)=1-t -> 75% source alpha.
    assert pixel.r > 0.99
    assert 0.23 < pixel.g < 0.27
    assert 0.23 < pixel.b < 0.27


def test_soft_mask_graphics_state_is_restored_by_q_Q():
    builder = PDFBuilder(version="1.7")
    mask_group = _add_form(
        builder,
        b"0 0 0 rg 0 0 50 100 re f\n",
        isolated=True,
    )
    mask = PDFDict({"S": PDFName("Alpha"), "G": mask_group})
    resources = PDFDict(
        {
            "ExtGState": PDFDict(
                {
                    "Mask": PDFDict({"SMask": mask}),
                    "NoMask": PDFDict({"SMask": PDFName("None")}),
                }
            )
        }
    )
    pdf = _finish(
        builder,
        b"/Mask gs q /NoMask gs 0 0 1 rg 0 0 100 100 re f Q "
        b"1 0 0 rg 0 0 100 100 re f\n",
        resources,
    )
    page = render_page(pdf, dpi=72)
    left = _pixel(page, 25, 50)
    right = _pixel(page, 75, 50)
    assert left.r > 0.99 and left.g < 0.02 and left.b < 0.02
    assert right.r < 0.02 and right.g < 0.02 and right.b > 0.99


def test_nonisolated_general_transparency_group_fails_closed():
    builder = PDFBuilder(version="1.7")
    form = _add_form(
        builder,
        b"1 0 0 rg 0 0 100 100 re f\n",
        isolated=False,
    )
    pdf = _finish(
        builder,
        b"/Fm Do\n",
        PDFDict({"XObject": PDFDict({"Fm": form})}),
    )
    try:
        render_page(pdf, dpi=72)
    except UnsupportedRenderingError as exc:
        assert "non-isolated" in str(exc)
    else:
        raise AssertionError("non-isolated transparency group must fail closed")


def test_knockout_transparency_group_fails_closed():
    builder = PDFBuilder(version="1.7")
    form = _add_form(
        builder,
        b"1 0 0 rg 0 0 100 100 re f\n",
        isolated=True,
        knockout=True,
    )
    pdf = _finish(
        builder,
        b"/Fm Do\n",
        PDFDict({"XObject": PDFDict({"Fm": form})}),
    )
    try:
        render_page(pdf, dpi=72)
    except UnsupportedRenderingError as exc:
        assert "knockout" in str(exc)
    else:
        raise AssertionError("knockout transparency group must fail closed")
