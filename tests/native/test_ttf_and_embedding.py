from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.font_embed import FontEmbeddingError, FontProgramMap, embed_missing_fonts
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.structure import resolve, walk_pages
from pdf2pdfa.native.ttf import SFNTFont
from tests.native.font_fixture import make_test_ttf


def _font_pdf() -> bytes:
    builder = PDFBuilder(version="1.4")
    font = PDFDict(
        {
            "Type": PDFName("Font"),
            "Subtype": PDFName("TrueType"),
            "BaseFont": PDFName("OwnedTestFont"),
            "Encoding": PDFName("WinAnsiEncoding"),
            "FirstChar": 65,
            "LastChar": 65,
            "Widths": [600],
        }
    )
    font_ref = builder.add(font)
    content_ref = builder.add(PDFStream(PDFDict(), b"BT /F1 12 Tf (A) Tj ET\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 200, 200],
                "Resources": PDFDict({"Font": PDFDict({"F1": font_ref})}),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def test_owned_sfnt_parser_reads_identity_permissions_metrics_and_cmap():
    font = SFNTFont(make_test_ttf())
    assert font.is_truetype
    assert not font.is_cff
    assert font.postscript_name == "OwnedTestFont"
    assert font.family_name == "Owned Test Font"
    assert font.full_name == "Owned Test Font Regular"
    assert font.embeddable
    assert font.embedding_fstype == 0
    assert font.metrics.units_per_em == 1000
    assert font.metrics.pdf_ascent == 800
    assert font.metrics.pdf_descent == -200
    assert font.cmap()[0x41] == 1
    assert font.glyph_advance_1000(1) == 600


def test_restricted_embedding_is_refused():
    mapping = FontProgramMap()
    try:
        mapping.add(make_test_ttf(fs_type=0x0002))
    except FontEmbeddingError as exc:
        assert "forbids outline embedding" in str(exc)
    else:
        raise AssertionError("restricted font should have been rejected")


def test_exact_true_type_font_is_embedded_without_external_library():
    doc = PDFDocument.open(_font_pdf(), repair=False)
    mapping = FontProgramMap()
    mapping.add(make_test_ttf())

    report = embed_missing_fonts(doc, mapping)

    assert report.complete
    assert report.embedded == 1
    page = next(walk_pages(doc))
    fonts = resolve(doc, page.resources["Font"])
    assert isinstance(fonts, PDFDict)
    font = resolve(doc, fonts["F1"])
    assert isinstance(font, PDFDict)
    descriptor = resolve(doc, font["FontDescriptor"])
    assert isinstance(descriptor, PDFDict)
    program = resolve(doc, descriptor["FontFile2"])
    assert isinstance(program, PDFStream)
    assert program.data == make_test_ttf()


def test_font_identity_mismatch_is_not_substituted():
    doc = PDFDocument.open(_font_pdf(), repair=False)
    mapping = FontProgramMap()
    mapping.add(make_test_ttf(postscript_name="AnotherFont"))
    report = embed_missing_fonts(doc, mapping)
    assert not report.complete
    assert "OwnedTestFont" in report.missing
