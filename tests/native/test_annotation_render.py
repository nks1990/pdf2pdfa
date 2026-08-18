from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full


def _appearance(
    content: bytes,
    *,
    bbox=(0, 0, 10, 10),
    matrix=None,
    resources: PDFDict | None = None,
) -> PDFStream:
    dictionary = PDFDict(
        {
            "Type": PDFName("XObject"),
            "Subtype": PDFName("Form"),
            "BBox": list(bbox),
            "Resources": resources or PDFDict(),
        }
    )
    if matrix is not None:
        dictionary["Matrix"] = list(matrix)
    return PDFStream(dictionary, content)


def _pdf(
    normal,
    *,
    rect=(20, 20, 40, 40),
    as_name: str | None = None,
    flags: int = 4,
    page_content: bytes = b"",
) -> bytes:
    builder = PDFBuilder(version="1.7")
    if isinstance(normal, PDFStream):
        n_value = builder.add(normal)
    else:
        n_value = PDFDict(
            {
                name: builder.add(stream)
                for name, stream in normal.items()
            }
        )
    ap = PDFDict({"N": n_value})
    annot = PDFDict(
        {
            "Type": PDFName("Annot"),
            "Subtype": PDFName("Stamp"),
            "Rect": list(rect),
            "F": flags,
            "AP": ap,
        }
    )
    if as_name is not None:
        annot["AS"] = PDFName(as_name)
    annot_ref = builder.add(annot)
    content_ref = builder.add(PDFStream(PDFDict(), page_content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(),
                "Contents": content_ref,
                "Annots": [annot_ref],
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_normal_appearance_maps_bbox_to_annotation_rect():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    page = render_page_full(_pdf(ap), dpi=72)
    inside = _pixel(page, 30, 30)
    outside = _pixel(page, 10, 10)
    assert inside.r > 0.98 and inside.g < 0.02 and inside.b < 0.02
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_appearance_matrix_is_preserved_in_rect_mapping_without_double_application():
    ap = _appearance(
        b"0 1 0 rg 0 0 5 10 re f\n",
        matrix=(2, 0, 0, 2, 5, 5),
    )
    page = render_page_full(_pdf(ap), dpi=72)
    left = _pixel(page, 25, 30)
    right = _pixel(page, 35, 30)
    assert left.g > 0.98 and left.r < 0.02
    assert right.r > 0.99 and right.g > 0.99 and right.b > 0.99


def test_stateful_normal_appearance_uses_annotation_as_name():
    normal = {
        "On": _appearance(b"0 0 1 rg 0 0 10 10 re f\n"),
        "Off": _appearance(b"1 0 0 rg 0 0 10 10 re f\n"),
    }
    page = render_page_full(_pdf(normal, as_name="On"), dpi=72)
    pixel = _pixel(page, 30, 30)
    assert pixel.b > 0.98 and pixel.r < 0.02 and pixel.g < 0.02


def test_hidden_or_noview_annotation_is_not_painted():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    for flags in (4 | 2, 4 | 32):
        page = render_page_full(_pdf(ap, flags=flags), dpi=72)
        pixel = _pixel(page, 30, 30)
        assert pixel.r > 0.99 and pixel.g > 0.99 and pixel.b > 0.99


def test_page_final_ctm_and_color_do_not_leak_into_annotation_state():
    ap = _appearance(b"0 0 10 10 re f\n")  # default fill must be black
    page = render_page_full(
        _pdf(
            ap,
            page_content=b"1 0 0 rg 2 0 0 2 50 50 cm\n",
        ),
        dpi=72,
    )
    pixel = _pixel(page, 30, 30)
    assert pixel.r < 0.02 and pixel.g < 0.02 and pixel.b < 0.02


def test_annotation_rendering_can_be_disabled_for_page_content_flattening():
    ap = _appearance(b"1 0 0 rg 0 0 10 10 re f\n")
    page = render_page_full(_pdf(ap), dpi=72, render_annotations=False)
    pixel = _pixel(page, 30, 30)
    assert pixel.r > 0.99 and pixel.g > 0.99 and pixel.b > 0.99


def test_transparent_appearance_blends_against_actual_page_backdrop():
    gs = PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})
    ap = _appearance(
        b"/Half gs 1 0 0 rg 0 0 10 10 re f\n",
        resources=PDFDict({"ExtGState": PDFDict({"Half": gs})}),
    )
    page = render_page_full(
        _pdf(ap, page_content=b"0 0 1 rg 0 0 100 100 re f\n"),
        dpi=72,
    )
    pixel = _pixel(page, 30, 30)
    assert 0.45 < pixel.r < 0.55
    assert pixel.g < 0.05
    assert 0.45 < pixel.b < 0.55
