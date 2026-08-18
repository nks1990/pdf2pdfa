"""Tests for multi-level PDF/A support (1b, 2b, 3b)."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pikepdf
import pytest

from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / "data"
XMP_NS = {"pdfaid": "http://www.aiim.org/pdfa/ns/id/"}


def _get_pdfa_fields(output_pdf: Path) -> tuple[str | None, str | None]:
    """Extract pdfaid:part and pdfaid:conformance from output XMP."""
    with pikepdf.Pdf.open(str(output_pdf)) as pdf:
        metadata = pdf.Root.Metadata.read_bytes()
    root = ET.fromstring(metadata)
    part = root.find(".//pdfaid:part", namespaces=XMP_NS)
    conf = root.find(".//pdfaid:conformance", namespaces=XMP_NS)
    return (
        part.text if part is not None else None,
        conf.text if conf is not None else None,
    )


@pytest.mark.parametrize(
    "level, expected_part",
    [("1b", "1"), ("2b", "2"), ("3b", "3")],
)
def test_convert_levels(tmp_path, level, expected_part):
    output = tmp_path / f"out_{level}.pdf"
    Converter(level=level).convert(str(DATA_DIR / "sample.pdf"), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == expected_part
    assert conf == "B"


def test_default_level_is_1b(tmp_path):
    output = tmp_path / "out.pdf"
    Converter().convert(str(DATA_DIR / "sample.pdf"), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == "1"
    assert conf == "B"


@pytest.mark.parametrize("level", ["4a", "2a", "1u", "banana"])
def test_invalid_level_raises(level):
    with pytest.raises(ValueError, match="Invalid PDF/A level"):
        Converter(level=level)


def test_output_intent_s_value(tmp_path):
    """All supported PDF/A levels use /GTS_PDFA1 in OutputIntent /S."""
    for level in ("1b", "2b", "3b"):
        output = tmp_path / f"intent_{level}.pdf"
        Converter(level=level).convert(str(DATA_DIR / "sample.pdf"), str(output))
        with pikepdf.Pdf.open(str(output)) as pdf:
            intent = pdf.Root.OutputIntents[0]
            assert str(intent["/S"]) == "/GTS_PDFA1"


def test_level_case_insensitive(tmp_path):
    output = tmp_path / "out.pdf"
    Converter(level="2B").convert(str(DATA_DIR / "sample.pdf"), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == "2"
    assert conf == "B"
