from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.cff_pdf_font import _type0_cmap as cff_type0_cmap
from pdf2pdfa.native.cmap import CIDCMap
from pdf2pdfa.native.cmap_registry import predefined_cmap
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.pdf_font import _type0_cmap as truetype_type0_cmap


def _doc() -> PDFDocument:
    builder = PDFBuilder(version="1.7")
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 0, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False)


def test_90ms_horizontal_matches_adobe_spot_values():
    cmap = predefined_cmap("90ms-RKSJ-H")
    assert not cmap.vertical
    assert cmap.code_to_cid(b"\x00") == 231  # Adobe beginnotdefrange
    assert cmap.code_to_cid(b"\x01") == 231
    assert cmap.code_to_cid(b" ") == 231
    assert cmap.code_to_cid(b"A") == 264
    assert cmap.code_to_cid(b"~") == 631
    assert cmap.code_to_cid(bytes.fromhex("8140")) == 633
    assert cmap.code_to_cid(bytes.fromhex("829f")) == 842
    assert cmap.code_to_cid(bytes.fromhex("889f")) == 1125
    assert cmap.code_to_cid(bytes.fromhex("fc4b")) == 8717
    assert cmap.code_to_cid(b"\xa0") == 326
    assert cmap.code_to_cid(b"\xdf") == 389


def test_90ms_vertical_inherits_horizontal_and_overrides_vertical_forms():
    horizontal = predefined_cmap("90ms-RKSJ-H")
    vertical = predefined_cmap("90ms-RKSJ-V")
    assert vertical.vertical
    assert vertical.code_to_cid(b"A") == horizontal.code_to_cid(b"A") == 264
    assert horizontal.code_to_cid(bytes.fromhex("8141")) == 634
    assert vertical.code_to_cid(bytes.fromhex("8141")) == 7887
    assert vertical.code_to_cid(bytes.fromhex("8142")) == 7888
    assert vertical.code_to_cid(bytes.fromhex("829f")) == 7918


def test_predefined_registry_is_cached_and_has_no_system_fallback():
    assert predefined_cmap("90ms-RKSJ-H") is predefined_cmap("90ms-RKSJ-H")
    try:
        predefined_cmap("Definitely-Not-Owned-H")
    except Exception as exc:
        assert "not present in the owned CMap registry" in str(exc)
    else:  # pragma: no cover - explicit contract assertion
        raise AssertionError("unknown predefined CMap must fail closed")


def test_embedded_cmap_can_usecmap_90ms_and_override_one_mapping():
    cmap = CIDCMap.parse(
        b"""
        /90ms-RKSJ-H usecmap
        1 begincidchar <8140> 9999 endcidchar
        """,
        registry=predefined_cmap,
    )
    assert cmap.code_to_cid(bytes.fromhex("8140")) == 9999
    assert cmap.code_to_cid(bytes.fromhex("8141")) == 634
    assert cmap.code_to_cid(b"A") == 264


def test_cff_and_truetype_bridges_share_compiled_predefined_cmap():
    doc = _doc()
    font = PDFDict({"Encoding": PDFName("90ms-RKSJ-V")})
    cff = cff_type0_cmap(doc, font)
    truetype = truetype_type0_cmap(doc, font)
    sample = bytes.fromhex("814141829f")
    expected = [
        (bytes.fromhex("8141"), 7887),
        (b"A", 264),
        (bytes.fromhex("829f"), 7918),
    ]
    assert cff.decode(sample) == expected
    assert truetype.decode(sample) == expected
    assert cff.vertical and truetype.vertical
