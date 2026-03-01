"""Tests for multi-level PDF/A support (1b, 2b, 3b)."""

from pathlib import Path

import pikepdf
import pytest
from lxml import etree

from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'

XMP_NS = {'pdfaid': 'http://www.aiim.org/pdfa/ns/id/'}


def _get_pdfa_fields(output_pdf: Path) -> tuple[str, str]:
    """Extract pdfaid:part and pdfaid:conformance from output XMP."""
    pdf = pikepdf.Pdf.open(str(output_pdf))
    metadata = pdf.Root.Metadata.read_bytes()
    root = etree.fromstring(metadata)
    part = root.find('.//pdfaid:part', namespaces=XMP_NS)
    conf = root.find('.//pdfaid:conformance', namespaces=XMP_NS)
    return (part.text if part is not None else None,
            conf.text if conf is not None else None)


def test_convert_level_1b(tmp_path):
    output = tmp_path / 'out.pdf'
    Converter(level="1b").convert(str(DATA_DIR / 'sample.pdf'), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == '1'
    assert conf == 'B'


def test_convert_level_2b(tmp_path):
    output = tmp_path / 'out.pdf'
    Converter(level="2b").convert(str(DATA_DIR / 'sample.pdf'), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == '2'
    assert conf == 'B'


def test_convert_level_3b(tmp_path):
    output = tmp_path / 'out.pdf'
    Converter(level="3b").convert(str(DATA_DIR / 'sample.pdf'), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == '3'
    assert conf == 'B'


def test_default_level_is_1b(tmp_path):
    output = tmp_path / 'out.pdf'
    Converter().convert(str(DATA_DIR / 'sample.pdf'), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == '1'
    assert conf == 'B'


def test_invalid_level_raises():
    with pytest.raises(ValueError, match="Invalid PDF/A level"):
        Converter(level="4a")


def test_invalid_level_2a_raises():
    with pytest.raises(ValueError, match="Invalid PDF/A level"):
        Converter(level="2a")


def test_output_intent_s_value(tmp_path):
    """All PDF/A levels must use /GTS_PDFA1 in OutputIntent /S."""
    for level in ("1b", "2b", "3b"):
        output = tmp_path / f'out_{level}.pdf'
        Converter(level=level).convert(str(DATA_DIR / 'sample.pdf'), str(output))
        pdf = pikepdf.Pdf.open(str(output))
        intent = pdf.Root.OutputIntents[0]
        assert str(intent['/S']) == '/GTS_PDFA1', f"Level {level}: expected /GTS_PDFA1"


def test_level_case_insensitive(tmp_path):
    output = tmp_path / 'out.pdf'
    Converter(level="2B").convert(str(DATA_DIR / 'sample.pdf'), str(output))
    part, conf = _get_pdfa_fields(output)
    assert part == '2'
    assert conf == 'B'
