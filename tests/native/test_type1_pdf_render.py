from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.page_render import RenderingError
from pdf2pdfa.native.type1_render import Type1TextPageRendererMixin

from tests.native.test_type1_core import _font, _notdef, _num, _pfa, _private, _rect


def _type1_font(program: bytes, *, encoding=None, first: int = 65, last: int = 65) -> PDFDict:
    fontfile = PDFStream(PDFDict(), program)
    descriptor = PDFDict(
        {
            "Type": PDFName("FontDescriptor"),
            "FontName": PDFName("OwnedType1"),
            "Flags": 32,
            "FontBBox": [0, 0, 700, 800],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
            "MissingWidth": 600,
            "FontFile": fontfile,
        }
    )
    font = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type1"),
            "BaseFont": PDFName("OwnedType1"),
            "FirstChar": first,
            "LastChar": last,
            "Widths": [600] * (last - first + 1),
            "Encoding": encoding or PDFName("WinAnsiEncoding"),
            "FontDescriptor": descriptor,
        }
    )
    return font


def _pdf(font: PDFDict, content: bytes, *, extra_resources: PDFDict | None = None, annots=None) -> bytes:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(font)
    resources = PDFDict({"Font": PDFDict({"F": font_ref})})
    if extra_resources:
        resources.update(extra_resources)
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = PDFDict(
        {
            "Type": PDFName("Page"),
            "Parent": pages_ref,
            "MediaBox": [0, 0, 100, 100],
            "Resources": resources,
            "Contents": contents,
        }
    )
    if annots:
        page["Annots"] = annots
    page_ref = builder.add(page)
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def _text(render_mode: int = 0) -> bytes:
    return (
        b"BT /F 60 Tf 1 0 0 1 10 10 Tm "
        + str(render_mode).encode("ascii")
        + b" Tr <41> Tj ET\n"
    )


def test_full_renderer_mro_includes_type1_bridge():
    assert Type1TextPageRendererMixin in FullOwnedPageRenderer.__mro__


@pytest.mark.parametrize("pfb", [False, True])
def test_embedded_type1_pfa_and_pfb_render_original_outline(pfb: bool):
    page = render_page_full(_pdf(_type1_font(_font(pfb=pfb)), _text()), dpi=72)
    inside = _bottom_pixel(page, 25, 35)
    outside = _bottom_pixel(page, 5, 35)
    assert inside.r < 0.05 and inside.g < 0.05 and inside.b < 0.05
    assert outside.r > 0.98 and outside.g > 0.98 and outside.b > 0.98


def test_type1_text_clip_mode_clips_following_paint():
    content = _text(7) + b"1 0 0 rg 0 0 100 100 re f\n"
    page = render_page_full(_pdf(_type1_font(_font()), content), dpi=72)
    inside = _bottom_pixel(page, 25, 35)
    outside = _bottom_pixel(page, 80, 80)
    assert inside.r > 0.95 and inside.g < 0.05 and inside.b < 0.05
    assert outside.r > 0.98 and outside.g > 0.98 and outside.b > 0.98


def test_type1_stroke_mode_uses_canonical_outline_text_machine():
    page = render_page_full(_pdf(_type1_font(_font()), b"2 w " + _text(1)), dpi=72)
    edge = _bottom_pixel(page, 16, 20)
    center = _bottom_pixel(page, 30, 35)
    assert edge.r < 0.3
    assert center.r > 0.9


def test_unsupported_seac_blocks_only_when_selected_glyph_is_painted():
    seac = (
        _num(0) + _num(600) + b"\x0d"
        + _num(0) + _num(10) + _num(20) + _num(65) + _num(39) + b"\x0c\x06"
    )
    program = _pfa(_private({".notdef": _notdef(), "A": _rect(), "B": seac}))
    encoding = PDFDict(
        {
            "BaseEncoding": PDFName("WinAnsiEncoding"),
            "Differences": [65, PDFName("A"), PDFName("B")],
        }
    )
    font = _type1_font(program, encoding=encoding, first=65, last=66)
    # The font itself is accepted and an ordinary glyph remains renderable.
    assert _bottom_pixel(render_page_full(_pdf(font, _text()), dpi=72), 25, 35).r < 0.05
    with pytest.raises(RenderingError, match="seac"):
        render_page_full(
            _pdf(font, b"BT /F 60 Tf 1 0 0 1 10 10 Tm <42> Tj ET\n"),
            dpi=72,
        )


def test_type1_inside_transparency_group_upgrades_group_text_renderer():
    font = _type1_font(_font())
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(font)
    form = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Form"),
                "BBox": [0, 0, 100, 100],
                "Group": PDFDict({"S": PDFName("Transparency"), "I": True, "K": False}),
                "Resources": PDFDict({"Font": PDFDict({"F": font_ref})}),
            }
        ),
        _text(),
    )
    form_ref = builder.add(form)
    content = builder.add(PDFStream(PDFDict(), b"/X Do\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"XObject": PDFDict({"X": form_ref})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    rendered = render_page_full(builder.to_bytes(), dpi=72)
    assert _bottom_pixel(rendered, 25, 35).r < 0.05


def test_type1_inside_annotation_appearance_upgrades_annotation_text_renderer():
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(_type1_font(_font()))
    appearance = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Form"),
                    "BBox": [0, 0, 100, 100],
                    "Resources": PDFDict({"Font": PDFDict({"F": font_ref})}),
                }
            ),
            _text(),
        )
    )
    annot = builder.add(
        PDFDict(
            {
                "Type": PDFName("Annot"),
                "Subtype": PDFName("Stamp"),
                "Rect": [0, 0, 100, 100],
                "F": 4,
                "AP": PDFDict({"N": appearance}),
            }
        )
    )
    content = builder.add(PDFStream(PDFDict(), b""))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(),
                "Contents": content,
                "Annots": [annot],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    rendered = render_page_full(builder.to_bytes(), dpi=72)
    assert _bottom_pixel(rendered, 25, 35).r < 0.05
