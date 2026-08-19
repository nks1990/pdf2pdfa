from __future__ import annotations

from pdf2pdfa.native.cmap import CIDCMap


def test_notdefrange_is_used_when_code_has_no_ordinary_cid_mapping():
    cmap = CIDCMap.parse(
        b"""
        /WMode 0 def
        1 begincodespacerange <00> <ff> endcodespacerange
        1 beginnotdefrange <00> <1f> 231 endnotdefrange
        1 begincidrange <20> <7e> 500 endcidrange
        """
    )
    assert cmap.code_to_cid(b"\x01") == 232
    assert cmap.code_to_cid(b" ") == 500


def test_notdefchar_is_fallback_not_override_of_inherited_regular_mapping():
    base = CIDCMap.parse(
        b"""
        1 begincodespacerange <00> <ff> endcodespacerange
        1 begincidchar <41> 100 endcidchar
        """
    )
    child = CIDCMap.parse(
        b"1 beginnotdefchar <41> 999 endnotdefchar",
        base=base,
    )
    assert child.code_to_cid(b"A") == 100


def test_child_regular_mapping_overrides_inherited_regular_mapping():
    base = CIDCMap.parse(
        b"""
        1 begincodespacerange <00> <ff> endcodespacerange
        1 begincidchar <41> 100 endcidchar
        """
    )
    child = CIDCMap.parse(
        b"1 begincidchar <41> 200 endcidchar",
        base=base,
    )
    assert child.code_to_cid(b"A") == 200


def test_child_notdef_precedes_inherited_notdef_when_no_regular_mapping_exists():
    base = CIDCMap.parse(
        b"""
        1 begincodespacerange <00> <ff> endcodespacerange
        1 beginnotdefrange <00> <ff> 700 endnotdefrange
        """
    )
    child = CIDCMap.parse(
        b"1 beginnotdefchar <41> 900 endnotdefchar",
        base=base,
    )
    assert child.code_to_cid(b"A") == 900
