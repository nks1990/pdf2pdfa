from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdf_font import PDFTextFont
from pdf2pdfa.native.raster import Color, Matrix, Surface
from pdf2pdfa.native.text_render import TextPaintStyle, TrueTypeTextRenderer
from tests.native.font_fixture import make_test_ttf


def _font_doc() -> tuple[PDFDocument, object]:
    builder = PDFBuilder(version="1.7")
    font_data = make_test_ttf()
    fontfile = builder.add(PDFStream(PDFDict({"Length1": len(font_data)}), font_data))
    descriptor = builder.add(
        PDFDict(
            {
                "Type": PDFName("FontDescriptor"),
                "FontName": PDFName("OwnedTestFont"),
                "FontFile2": fontfile,
            }
        )
    )
    font_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Font"),
                "Subtype": PDFName("TrueType"),
                "BaseFont": PDFName("OwnedTestFont"),
                "Encoding": PDFName("WinAnsiEncoding"),
                "FirstChar": 65,
                "LastChar": 65,
                "Widths": [550],
                "FontDescriptor": descriptor,
            }
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({"Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,100,100]}))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False), font_ref


def test_show_text_rasterizes_true_type_outline_and_advances_by_pdf_width():
    doc, font_ref = _font_doc()
    font = PDFTextFont(doc, font_ref)
    surface = Surface(40, 40)
    renderer = TrueTypeTextRenderer(surface, ctm=Matrix())
    renderer.begin_text()
    renderer.set_font(font, 10)
    renderer.set_text_matrix(Matrix(1, 0, 0, 1, 10, 10))
    renderer.show(
        b"A",
        TextPaintStyle(
            fill=Color(0, 0, 0, 1),
            stroke=Color(0, 0, 0, 1),
            line_width=1,
        ),
    )
    renderer.end_text()

    # Synthetic glyph is a filled 600x700 unit rectangle at 10pt => 6x7 pixels.
    assert surface.get_pixel(12, 12).a > 0.9
    assert surface.get_pixel(9, 12).a == 0
    assert abs(renderer.state.text_matrix.e - 15.5) < 1e-6


def test_tj_numeric_adjustment_updates_text_matrix():
    doc, font_ref = _font_doc()
    font = PDFTextFont(doc, font_ref)
    surface = Surface(40, 40)
    renderer = TrueTypeTextRenderer(surface, ctm=Matrix())
    renderer.begin_text()
    renderer.set_font(font, 10)
    renderer.show_array(
        [b"A", 100, b"A"],
        TextPaintStyle(Color(0,0,0,1), Color(0,0,0,1), 1),
    )
    # 5.5 - 1.0 + 5.5 = 10.0 units.
    assert abs(renderer.state.text_matrix.e - 10.0) < 1e-6


def test_text_clipping_mode_intersects_surface_clip_at_et():
    doc, font_ref = _font_doc()
    font = PDFTextFont(doc, font_ref)
    surface = Surface(20, 20)
    renderer = TrueTypeTextRenderer(surface, ctm=Matrix())
    renderer.begin_text()
    renderer.set_font(font, 10)
    renderer.set_text_matrix(Matrix(1, 0, 0, 1, 5, 5))
    renderer.set_render_mode(7)
    renderer.show(
        b"A",
        TextPaintStyle(Color(0,0,0,1), Color(0,0,0,1), 1),
    )
    renderer.end_text()
    surface.composite_pixel(7, 7, Color(1, 0, 0, 1))
    surface.composite_pixel(2, 2, Color(1, 0, 0, 1))
    assert surface.get_pixel(7, 7).a == 1.0
    assert surface.get_pixel(2, 2).a == 0.0
