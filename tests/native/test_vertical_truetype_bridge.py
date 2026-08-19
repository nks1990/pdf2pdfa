from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdf_font import PDFTextFont
from pdf2pdfa.native.vertical_metrics import VerticalMetric

from tests.native.font_fixture import make_test_ttf


def _document_and_font(*, dw2=None, w2=None) -> tuple[PDFDocument, PDFDict]:
    descriptor = PDFDict(
        {
            "Type": PDFName("FontDescriptor"),
            "FontName": PDFName("OwnedTestFont"),
            "Flags": 4,
            "FontBBox": [0, -200, 600, 800],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
            "FontFile2": PDFStream(PDFDict(), make_test_ttf()),
        }
    )
    descendant = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("CIDFontType2"),
            "BaseFont": PDFName("OwnedTestFont"),
            "CIDSystemInfo": PDFDict(
                {"Registry": b"Adobe", "Ordering": b"Identity", "Supplement": 0}
            ),
            "FontDescriptor": descriptor,
            "DW": 600,
            "W": [1, [600]],
            "CIDToGIDMap": PDFName("Identity"),
        }
    )
    if dw2 is not None:
        descendant["DW2"] = dw2
    if w2 is not None:
        descendant["W2"] = w2
    type0 = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("Type0"),
            "BaseFont": PDFName("OwnedTestFont"),
            "Encoding": PDFName("Identity-V"),
            "DescendantFonts": [descendant],
        }
    )

    builder = PDFBuilder(version="1.7")
    pages_ref = builder.add(PDFDict({"Type": PDFName("Pages"), "Count": 0, "Kids": []}))
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False), type0


def test_cidfonttype2_identity_v_attaches_default_vertical_metric():
    doc, font_dict = _document_and_font()
    font = PDFTextFont(doc, font_dict)
    items = font.decode(b"\x00\x01")
    assert font.vertical
    assert len(items) == 1
    assert items[0].cid == 1
    assert items[0].glyph_id == 1
    assert items[0].vertical_metric == VerticalMetric(-1000.0, 300.0, 880.0)


def test_cidfonttype2_uses_same_dw2_w2_override_as_cff_bridge():
    doc, font_dict = _document_and_font(
        dw2=[900, -1200],
        w2=[1, [-700, 250, 750]],
    )
    item = PDFTextFont(doc, font_dict).decode(b"\x00\x01")[0]
    assert item.vertical_metric == VerticalMetric(-700.0, 250.0, 750.0)
