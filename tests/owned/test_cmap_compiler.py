from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.cmap import CIDCMap, CIDRange, CodeSpace, NotDefRange
from scripts.compile_cmaps import compile_resources, render_module


def _write(root: Path, collection: str, name: str, body: bytes) -> None:
    target = root / collection / "CMap" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def test_compiler_preserves_point_range_notdef_and_vertical_inheritance(tmp_path: Path):
    _write(
        tmp_path,
        "Adobe-Test-0",
        "Test-H",
        b"""
        /WMode 0 def
        1 begincodespacerange <00> <ff> endcodespacerange
        1 beginnotdefrange <00> <1f> 7 endnotdefrange
        1 begincidchar <41> 100 endcidchar
        1 begincidrange <50> <52> 200 endcidrange
        """,
    )
    _write(
        tmp_path,
        "Adobe-Test-0",
        "Test-V",
        b"""
        /Test-H usecmap
        /WMode 1 def
        1 begincidchar <41> 900 endcidchar
        """,
    )

    data, hashes = compile_resources(tmp_path, ["Test-V"])
    assert list(data) == ["Test-H", "Test-V"]
    assert set(hashes) == {"Test-H", "Test-V"}

    horizontal = data["Test-H"]
    assert horizontal["notdef_ranges"] == ((0x00, 0x1F, 1, 7),)
    assert horizontal["cid_chars"] == ((0x41, 1, 100),)
    assert horizontal["cid_ranges"] == ((0x50, 0x52, 1, 200),)

    vertical = data["Test-V"]
    assert vertical["vertical"] is True
    assert vertical["base"] == "Test-H"
    assert vertical["cid_chars"] == ((0x41, 1, 900),)


def test_compiler_output_is_byte_deterministic_for_same_inputs(tmp_path: Path):
    _write(
        tmp_path,
        "Adobe-Test-0",
        "B-H",
        b"1 begincodespacerange <00> <ff> endcodespacerange\n",
    )
    _write(
        tmp_path,
        "Adobe-Test-0",
        "A-H",
        b"1 begincodespacerange <00> <ff> endcodespacerange\n",
    )
    data1, hashes1 = compile_resources(tmp_path, ["B-H", "A-H"])
    data2, hashes2 = compile_resources(tmp_path, ["A-H", "B-H"])
    assert render_module(data1, hashes1) == render_module(data2, hashes2)
