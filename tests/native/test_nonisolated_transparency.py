from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.nonisolated_transparency import NonIsolatedTransparencyRendererMixin
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.page_render import UnsupportedRenderingError

from tests.native.test_cff_pdf_render import _simple_cff, _simple_font


def _form(
    content: bytes,
    *,
    resources: PDFDict | None = None,
    isolated: bool,
    knockout: bool = False,
    cs=None,
) -> PDFStream:
    group = PDFDict(
        {"S": PDFName("Transparency"), "I": isolated, "K": knockout}
    )
    if cs is not None:
        group["CS"] = cs
    return PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Form"),
                "BBox": [0, 0, 100, 100],
                "Resources": resources or PDFDict(),
                "Group": group,
            }
        ),
        content,
    )


def _pdf(form: PDFStream, *, prefix: bytes = b"", boundary_gs: PDFDict | None = None) -> bytes:
    builder = PDFBuilder(version="1.7")
    form_ref = builder.add(form)
    resources = PDFDict({"XObject": PDFDict({"Fm": form_ref})})
    content = prefix
    if boundary_gs is not None:
        resources["ExtGState"] = PDFDict({"Boundary": boundary_gs})
        content += b"/Boundary gs "
    content += b"/Fm Do\n"
    content_ref = builder.add(PDFStream(PDFDict(), content))
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
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x=50, y=50):
    return page.surface.get_pixel(x, page.height - 1 - y)


def _multiply_half_form(*, isolated: bool) -> PDFStream:
    resources = PDFDict(
        {
            "ExtGState": PDFDict(
                {
                    "MH": PDFDict(
                        {
                            "Type": PDFName("ExtGState"),
                            "ca": 0.5,
                            "BM": PDFName("Multiply"),
                        }
                    )
                }
            )
        }
    )
    return _form(
        b"/MH gs 1 0 0 rg 0 0 100 100 re f\n",
        resources=resources,
        isolated=isolated,
    )


def test_full_renderer_mro_includes_nonisolated_group_layer():
    assert NonIsolatedTransparencyRendererMixin in FullOwnedPageRenderer.__mro__


def test_nonisolated_internal_multiply_uses_real_blue_backdrop():
    prefix = b"0 0 1 rg 0 0 100 100 re f\n"
    nonisolated = render_page_full(
        _pdf(_multiply_half_form(isolated=False), prefix=prefix), dpi=72
    )
    isolated = render_page_full(
        _pdf(_multiply_half_form(isolated=True), prefix=prefix), dpi=72
    )
    ni = _pixel(nonisolated)
    iso = _pixel(isolated)

    # Non-isolated Multiply sees blue inside the group: red*blue -> black,
    # then 50% source alpha leaves half blue.
    assert ni.r < 0.05 and ni.g < 0.05 and 0.45 < ni.b < 0.55
    # Isolated group has no blue backdrop while its red source is painted;
    # normal group-boundary composition therefore yields purple instead.
    assert 0.45 < iso.r < 0.55 and iso.g < 0.05 and 0.45 < iso.b < 0.55


def test_nonisolated_boundary_alpha_is_applied_once():
    prefix = b"0 0 1 rg 0 0 100 100 re f\n"
    page = render_page_full(
        _pdf(
            _multiply_half_form(isolated=False),
            prefix=prefix,
            boundary_gs=PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5}),
        ),
        dpi=72,
    )
    pixel = _pixel(page)
    # Effective group source is black at alpha .5; boundary .5 makes alpha .25.
    assert pixel.r < 0.05 and pixel.g < 0.05
    assert 0.70 < pixel.b < 0.80


def test_nested_nonisolated_group_handles_translucent_backdrop_alpha():
    inner = _multiply_half_form(isolated=False)
    inner_resources = PDFDict({"XObject": PDFDict({"Inner": inner})})
    inner_resources["ExtGState"] = PDFDict(
        {"HalfBlue": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})}
    )
    outer = _form(
        b"/HalfBlue gs 0 0 1 rg 0 0 100 100 re f /Inner Do\n",
        resources=inner_resources,
        isolated=True,
    )
    page = render_page_full(_pdf(outer), dpi=72)
    pixel = _pixel(page)
    # Derived from source-over on a 0.5-alpha blue backdrop, then the isolated
    # outer group is composed over the page's white background.
    assert 0.45 < pixel.r < 0.55
    assert 0.20 < pixel.g < 0.30
    assert 0.45 < pixel.b < 0.55


def test_cff_text_renders_inside_isolated_group_with_canonical_text_renderer():
    font = _simple_font(_simple_cff(b"A"), encoding=PDFName("WinAnsiEncoding"))
    form = _form(
        b"BT /F 60 Tf 1 0 0 1 10 10 Tm <41> Tj ET\n",
        resources=PDFDict({"Font": PDFDict({"F": font})}),
        isolated=True,
    )
    page = render_page_full(_pdf(form), dpi=72)
    painted = [
        page.surface.get_pixel(x, page.height - 1 - y)
        for y in range(10, 70)
        for x in range(10, 50)
    ]
    assert any(pixel.r < 0.1 and pixel.g < 0.1 and pixel.b < 0.1 for pixel in painted)


def test_explicit_non_rgb_group_blending_space_remains_fail_closed():
    form = _form(
        b"0 0 0 1 k 0 0 100 100 re f\n",
        isolated=False,
        cs=PDFName("DeviceCMYK"),
    )
    with pytest.raises(UnsupportedRenderingError, match="non-RGB transparency-group"):
        render_page_full(_pdf(form), dpi=72)


def test_knockout_group_still_fails_closed():
    form = _form(b"1 0 0 rg 0 0 100 100 re f\n", isolated=False, knockout=True)
    with pytest.raises(UnsupportedRenderingError, match="knockout"):
        render_page_full(_pdf(form), dpi=72)
