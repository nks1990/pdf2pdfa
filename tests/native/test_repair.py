from __future__ import annotations

from pathlib import Path
import zlib

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.color_profiles import generic_cmyk_profile_bytes
from pdf2pdfa.native.icc import parse_icc
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.repair import NativeRepairEngine, UnsupportedNativeRepairError


def _input(
    *,
    version: str = "1.7",
    content: bytes = b"q Q\n",
    resources: PDFDict | None = None,
    catalog_extra: PDFDict | None = None,
    compressed_content: bool = False,
) -> bytes:
    builder = PDFBuilder(version=version)
    if compressed_content:
        content_stream = PDFStream(
            PDFDict({"Filter": PDFName("FlateDecode")}), zlib.compress(content)
        )
    else:
        content_stream = PDFStream(PDFDict(), content)
    content_ref = builder.add(content_stream)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 200, 200],
                "Resources": resources or PDFDict(),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    catalog = PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref})
    if catalog_extra:
        catalog.update(catalog_extra)
    root = builder.add(catalog)
    builder.set_root(root)
    return builder.to_bytes()


def test_generated_cmyk_profile_has_both_directions():
    profile = parse_icc(generic_cmyk_profile_bytes(grid_points=5))
    assert profile.color_space == "CMYK"
    assert profile.profile_class == "prtr"
    assert profile.has_device_to_pcs
    assert profile.has_pcs_to_device
    assert "A2B0" in profile.tags
    assert "B2A0" in profile.tags


def test_native_repair_converts_plain_pdf_to_pdfa2b(tmp_path: Path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "archive.pdf"
    source.write_bytes(_input())

    result = NativeRepairEngine().convert(source, destination, "2b")

    assert result.validation.compliant, result.validation.failures
    assert destination.is_file()
    assert NativePDFAValidator().validate(destination, "2b").compliant
    assert b"pdf2pdfa owned engine" in destination.read_bytes()


def test_native_repair_removes_javascript(tmp_path: Path):
    source = tmp_path / "js.pdf"
    destination = tmp_path / "archive.pdf"
    source.write_bytes(
        _input(
            catalog_extra=PDFDict(
                {
                    "OpenAction": PDFDict(
                        {"S": PDFName("JavaScript"), "JS": b"app.alert('x')"}
                    )
                }
            )
        )
    )

    result = NativeRepairEngine().convert(source, destination, "2b")
    assert result.validation.compliant, result.validation.failures
    assert b"JavaScript" not in destination.read_bytes()


def test_native_repair_characterizes_device_cmyk(tmp_path: Path):
    source = tmp_path / "cmyk.pdf"
    destination = tmp_path / "archive.pdf"
    source.write_bytes(_input(content=b"0 0 0 1 k\n0 0 100 100 re f\n"))

    result = NativeRepairEngine().convert(source, destination, "2b")
    assert result.validation.compliant, result.validation.failures
    assert b"pdf2pdfa generic CMYK" in destination.read_bytes()


def test_pdfa1_transparency_is_a_blocker_until_owned_flattening_exists(tmp_path: Path):
    resources = PDFDict(
        {
            "ExtGState": PDFDict(
                {"GS0": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})}
            )
        }
    )
    source = tmp_path / "transparent.pdf"
    source.write_bytes(_input(content=b"/GS0 gs\n", resources=resources))
    plan = NativeRepairEngine().plan(source, "1b")
    assert not plan.repairable
    assert any(item.code == "pdfa1.transparency" for item in plan.blockers)


def test_failed_repair_preserves_existing_destination(tmp_path: Path):
    resources = PDFDict(
        {
            "ExtGState": PDFDict(
                {"GS0": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5})}
            )
        }
    )
    source = tmp_path / "transparent.pdf"
    destination = tmp_path / "existing.pdf"
    source.write_bytes(_input(content=b"/GS0 gs\n", resources=resources))
    destination.write_bytes(b"sentinel")

    try:
        NativeRepairEngine().convert(source, destination, "1b")
    except UnsupportedNativeRepairError:
        pass
    else:
        raise AssertionError("expected transparency repair blocker")

    assert destination.read_bytes() == b"sentinel"
