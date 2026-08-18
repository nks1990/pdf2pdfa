from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.page_render import UnsupportedRenderingError, render_page
from tests.native.font_fixture import make_test_ttf


def _pdf(content: bytes, resources: PDFDict | None = None) -> bytes:
    builder = PDFBuilder(version="1.7")
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref,
        "MediaBox": [0,0,100,100], "CropBox": [0,0,100,100],
        "Resources": resources or PDFDict(), "Contents": content_ref,
    }))
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_vector_fill_reaches_expected_device_pixel():
    page = render_page(_pdf(b"0 0 1 rg 10 10 20 20 re f\n"), dpi=72)
    inside = page.surface.get_pixel(15, 85)
    outside = page.surface.get_pixel(5, 85)
    assert inside.b > 0.99 and inside.r < 0.01 and inside.g < 0.01
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_extgstate_fill_alpha_composites_over_white():
    resources = PDFDict({
        "ExtGState": PDFDict({"GS": PDFDict({"ca": 0.5})}),
    })
    page = render_page(_pdf(b"/GS gs 1 0 0 rg 10 10 20 20 re f\n", resources), dpi=72)
    pixel = page.surface.get_pixel(15, 85)
    assert pixel.r > 0.99
    assert 0.48 < pixel.g < 0.52
    assert 0.48 < pixel.b < 0.52


def test_true_type_text_is_rasterized_by_owned_glyf_engine():
    builder = PDFBuilder(version="1.7")
    font_data = make_test_ttf()
    font_file = builder.add(PDFStream(PDFDict({"Length1": len(font_data)}), font_data))
    descriptor = builder.add(PDFDict({
        "Type": PDFName("FontDescriptor"), "FontName": PDFName("OwnedTestFont"),
        "FontFile2": font_file,
    }))
    font = builder.add(PDFDict({
        "Type": PDFName("Font"), "Subtype": PDFName("TrueType"),
        "BaseFont": PDFName("OwnedTestFont"), "Encoding": PDFName("WinAnsiEncoding"),
        "FirstChar": 65, "LastChar": 65, "Widths": [550],
        "FontDescriptor": descriptor,
    }))
    content = builder.add(PDFStream(PDFDict(), b"BT /F1 10 Tf 1 0 0 1 10 10 Tm (A) Tj ET\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,100,100],
        "Resources": PDFDict({"Font": PDFDict({"F1": font})}), "Contents": content,
    }))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    page = render_page(builder.to_bytes(), dpi=72)
    pixel = page.surface.get_pixel(12, 86)
    assert pixel.r < 0.1 and pixel.g < 0.1 and pixel.b < 0.1


def test_image_xobject_is_affinely_placed():
    builder = PDFBuilder(version="1.7")
    image = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 1, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceRGB"),
    }), bytes([255,0,0])))
    content = builder.add(PDFStream(PDFDict(), b"q 20 0 0 20 10 10 cm /Im1 Do Q\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,100,100],
        "Resources": PDFDict({"XObject": PDFDict({"Im1": image})}), "Contents": content,
    }))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    page = render_page(builder.to_bytes(), dpi=72)
    pixel = page.surface.get_pixel(15, 85)
    assert pixel.r > 0.99 and pixel.g < 0.01 and pixel.b < 0.01


def test_form_xobject_matrix_bbox_and_resources_are_applied():
    builder = PDFBuilder(version="1.7")
    form = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Form"),
        "BBox": [0,0,1,1], "Matrix": [20,0,0,20,10,10],
        "Resources": PDFDict(),
    }), b"0 1 0 rg 0 0 1 1 re f\n"))
    content = builder.add(PDFStream(PDFDict(), b"/Fm1 Do\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({
        "Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,100,100],
        "Resources": PDFDict({"XObject": PDFDict({"Fm1": form})}), "Contents": content,
    }))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    page = render_page(builder.to_bytes(), dpi=72)
    pixel = page.surface.get_pixel(15, 85)
    assert pixel.g > 0.99 and pixel.r < 0.01 and pixel.b < 0.01


def test_unsupported_dashed_stroke_fails_instead_of_rendering_solid():
    try:
        render_page(_pdf(b"[2 2] 0 d 1 w 10 10 m 40 10 l S\n"), dpi=72)
    except UnsupportedRenderingError as exc:
        assert "dash" in str(exc).lower()
    else:
        raise AssertionError("unsupported dashed stroke must fail closed")
