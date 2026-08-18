from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.cmap import CIDCMap
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdf_font import PDFTextFont
from pdf2pdfa.native.raster import Matrix
from pdf2pdfa.native.truetype import TrueTypeOutlines
from pdf2pdfa.native.ttf import SFNTFont
from tests.native.font_fixture import make_test_ttf


def _doc_with_font(font: PDFDict) -> tuple[PDFDocument, object]:
    builder = PDFBuilder(version="1.7")
    font_ref = builder.add(font)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F1": font_ref})}),
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False), font_ref


def _font_file(builderless: bool = True) -> PDFStream:
    return PDFStream(PDFDict({"Length1": len(make_test_ttf())}), make_test_ttf())


def test_true_type_square_outline_is_decoded_and_transformed():
    font = SFNTFont(make_test_ttf())
    outlines = TrueTypeOutlines(font)
    glyph = outlines.glyph(1)
    assert len(glyph.contours) == 1
    assert [(point.x, point.y, point.on_curve) for point in glyph.contours[0]] == [
        (0.0, 0.0, True),
        (600.0, 0.0, True),
        (600.0, 700.0, True),
        (0.0, 700.0, True),
    ]
    path = outlines.path(1, Matrix(0.01, 0, 0, 0.01, 2, 3))
    assert path.subpaths[0].points[0] == (2.0, 3.0)
    assert path.subpaths[0].points[1] == (8.0, 3.0)


def test_custom_cmap_maps_variable_codes_to_cids():
    cmap = CIDCMap.parse(
        b"""
        2 begincodespacerange
        <00> <7F>
        <8100> <81FF>
        endcodespacerange
        1 begincidchar
        <41> 7
        endcidchar
        1 begincidrange
        <8100> <8102> 20
        endcidrange
        """
    )
    assert cmap.decode(b"A\x81\x00\x81\x02") == [
        (b"A", 7),
        (b"\x81\x00", 20),
        (b"\x81\x02", 22),
    ]


def test_simple_true_type_pdf_font_maps_winansi_to_glyph_and_pdf_width():
    # Build manually so FontFile2 is an indirect stream in the parsed document.
    builder = PDFBuilder(version="1.7")
    fontfile = builder.add(PDFStream(PDFDict({"Length1": len(make_test_ttf())}), make_test_ttf()))
    descriptor = builder.add(
        PDFDict(
            {
                "Type": PDFName("FontDescriptor"),
                "FontName": PDFName("OwnedTestFont"),
                "FontFile2": fontfile,
            }
        )
    )
    font = PDFDict(
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
    font_ref = builder.add(font)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F1": font_ref})}),
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)

    parsed = PDFTextFont(doc, font_ref)
    items = parsed.decode(b"A")
    assert len(items) == 1
    assert items[0].glyph_id == 1
    assert items[0].width_1000 == 550


def test_type0_identity_h_uses_cid_to_gid_and_cid_widths():
    builder = PDFBuilder(version="1.7")
    fontfile = builder.add(PDFStream(PDFDict({"Length1": len(make_test_ttf())}), make_test_ttf()))
    descriptor = builder.add(
        PDFDict(
            {
                "Type": PDFName("FontDescriptor"),
                "FontName": PDFName("OwnedTestFont"),
                "FontFile2": fontfile,
            }
        )
    )
    cidfont = builder.add(
        PDFDict(
            {
                "Type": PDFName("Font"),
                "Subtype": PDFName("CIDFontType2"),
                "BaseFont": PDFName("OwnedTestFont"),
                "CIDSystemInfo": PDFDict(
                    {"Registry": b"Adobe", "Ordering": b"Identity", "Supplement": 0}
                ),
                "FontDescriptor": descriptor,
                "CIDToGIDMap": PDFName("Identity"),
                "DW": 1000,
                "W": [1, [620]],
            }
        )
    )
    type0 = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type0"),
            "BaseFont": PDFName("OwnedTestFont"),
            "Encoding": PDFName("Identity-H"),
            "DescendantFonts": [cidfont],
        }
    )
    type0_ref = builder.add(type0)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Font": PDFDict({"F1": type0_ref})}),
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)

    parsed = PDFTextFont(doc, type0_ref)
    items = parsed.decode(b"\x00\x01")
    assert len(items) == 1
    assert items[0].cid == 1
    assert items[0].glyph_id == 1
    assert items[0].width_1000 == 620
