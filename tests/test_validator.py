from __future__ import annotations

import pytest

from pdf2pdfa.validator import ValidationExecutionError, parse_verapdf_xml


def test_parse_compliant_verapdf_report():
    xml = """<?xml version='1.0'?>
    <report><validationReport isCompliant='true'>
      <details passedChecks='123' failedChecks='0'/>
    </validationReport></report>"""
    result = parse_verapdf_xml(xml, "2b")
    assert result.compliant is True
    assert result.flavour == "2b"
    assert result.failed_checks == 0
    assert result.passed_checks == 123


def test_parse_noncompliant_verapdf_report():
    xml = """<report><validationReport isCompliant='false'>
      <details passedChecks='10' failedChecks='2'/>
      <rule status='failed' clause='6.3.4'/>
    </validationReport></report>"""
    result = parse_verapdf_xml(xml, "1b")
    assert result.compliant is False
    assert result.failed_checks == 2
    assert result.failed_rules == ("6.3.4",)


def test_missing_validation_report_is_error():
    with pytest.raises(ValidationExecutionError):
        parse_verapdf_xml("<report />", "1b")
