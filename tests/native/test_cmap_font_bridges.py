from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.cff_pdf_font import _type0_cmap as cff_type0_cmap
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdf_font import _type0_cmap as truetype_type0_cmap


def _doc() -> PDFDocument:
    builder = PDFBuilder(version="1.7")
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 0, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False)


def test_cff_and_truetype_type0_use_identical_usecmap_semantics():
    encoding = PDFStream(
        PDFDict({"UseCMap": PDFName("Identity-H")}),
        b"1 begincidchar\n<0041> 700\nendcidchar\n",
    )
    font = PDFDict({"Encoding": encoding})
    doc = _doc()
    left = cff_type0_cmap(doc, font)
    right = truetype_type0_cmap(doc, font)
    sample = b"\x00A\x00B"
    assert left.decode(sample) == right.decode(sample) == [
        (b"\x00A", 700),
        (b"\x00B", 66),
    ]
    assert left.vertical == right.vertical == False


def test_cff_and_truetype_type0_share_vertical_identity_resolution():
    font = PDFDict({"Encoding": PDFName("Identity-V")})
    doc = _doc()
    assert cff_type0_cmap(doc, font).vertical
    assert truetype_type0_cmap(doc, font).vertical
