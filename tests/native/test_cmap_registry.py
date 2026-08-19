from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.cmap import CIDCMap, CMapError
from pdf2pdfa.native.cmap_registry import predefined_cmap, resolve_type0_cmap
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream


def _doc() -> PDFDocument:
    builder = PDFBuilder(version="1.7")
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 0, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False)


def test_identity_predefined_cmaps_are_algorithmic_and_vertical_is_explicit():
    horizontal = predefined_cmap("Identity-H")
    vertical = predefined_cmap("Identity-V")
    assert horizontal.decode(b"\x00A") == [(b"\x00A", 65)]
    assert not horizontal.vertical
    assert vertical.vertical
    with pytest.raises(CMapError, match="not present in the owned CMap registry"):
        predefined_cmap("90ms-RKSJ-H")


def test_content_usecmap_inherits_codespace_and_child_mapping_overrides_base():
    child = CIDCMap.parse(
        b"/Identity-H usecmap\n"
        b"1 begincidchar\n<0041> 777\nendcidchar\n",
        registry=predefined_cmap,
    )
    assert child.decode(b"\x00A\x00B") == [
        (b"\x00A", 777),
        (b"\x00B", 66),
    ]


def test_wmode_is_inherited_from_base_and_can_be_overridden_locally():
    inherited = CIDCMap.parse(
        b"/Identity-V usecmap\n",
        registry=predefined_cmap,
    )
    overridden = CIDCMap.parse(
        b"/WMode 0 def\n/Identity-V usecmap\n",
        registry=predefined_cmap,
    )
    assert inherited.vertical
    assert not overridden.vertical


def test_stream_dictionary_usecmap_name_is_resolved_by_shared_type0_resolver():
    stream = PDFStream(
        PDFDict({"UseCMap": PDFName("Identity-H")}),
        b"1 begincidchar\n<0041> 901\nendcidchar\n",
    )
    cmap = resolve_type0_cmap(_doc(), stream)
    assert cmap.decode(b"\x00A\x00B") == [
        (b"\x00A", 901),
        (b"\x00B", 66),
    ]


def test_stream_dictionary_usecmap_stream_chains_recursively():
    base = PDFStream(
        PDFDict({"UseCMap": PDFName("Identity-H")}),
        b"1 begincidchar\n<0041> 501\nendcidchar\n",
    )
    child = PDFStream(
        PDFDict({"UseCMap": base}),
        b"1 begincidchar\n<0042> 502\nendcidchar\n",
    )
    cmap = resolve_type0_cmap(_doc(), child)
    assert cmap.decode(b"\x00A\x00B\x00C") == [
        (b"\x00A", 501),
        (b"\x00B", 502),
        (b"\x00C", 67),
    ]


def test_usecmap_cycle_and_double_base_are_fail_closed():
    cyclic = PDFStream(PDFDict(), b"")
    cyclic.dictionary["UseCMap"] = cyclic
    with pytest.raises(CMapError, match="cycle"):
        resolve_type0_cmap(_doc(), cyclic)

    double = PDFStream(
        PDFDict({"UseCMap": PDFName("Identity-H")}),
        b"/Identity-H usecmap\n",
    )
    with pytest.raises(CMapError, match="both stream /UseCMap and content usecmap"):
        resolve_type0_cmap(_doc(), double)


def test_multiple_content_usecmap_operators_are_rejected():
    with pytest.raises(CMapError, match="more than one usecmap"):
        CIDCMap.parse(
            b"/Identity-H usecmap /Identity-H usecmap",
            registry=predefined_cmap,
        )


def test_usecmap_without_owned_registry_never_looks_at_system_resources():
    with pytest.raises(CMapError, match="requires an owned predefined-CMap registry"):
        CIDCMap.parse(b"/Identity-H usecmap")
