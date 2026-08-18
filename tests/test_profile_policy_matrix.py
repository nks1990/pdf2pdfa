from __future__ import annotations

from pdf2pdfa.orchestrator import _requires_full_rewrite
from pdf2pdfa.preflight import analyze_pdf
from tests.fixtures import (
    attachment_pdf,
    device_cmyk_image_pdf,
    device_rgb_image_pdf,
    direct_device_color_pdf,
    javascript_pdf,
    signature_pdf,
    transparency_pdf,
    type0_font_pdf,
)


def _codes(report):
    return {issue.code for issue in report.issues}


def test_transparency_differs_between_pdfa1_and_pdfa2(tmp_path):
    source = transparency_pdf(tmp_path / "transparency.pdf")
    assert "transparency" in _codes(analyze_pdf(source, "1b"))
    assert "transparency" not in _codes(analyze_pdf(source, "2b"))
    assert "transparency" not in _codes(analyze_pdf(source, "3b"))


def test_attachments_are_only_allowed_by_pdfa3(tmp_path):
    source = attachment_pdf(tmp_path / "attachment.pdf")
    assert "embedded_files" in _codes(analyze_pdf(source, "1b"))
    assert "embedded_files" in _codes(analyze_pdf(source, "2b"))
    assert "embedded_files" not in _codes(analyze_pdf(source, "3b"))


def test_javascript_is_rejected_by_all_supported_profiles(tmp_path):
    source = javascript_pdf(tmp_path / "javascript.pdf")
    for level in ("1b", "2b", "3b"):
        assert "javascript" in _codes(analyze_pdf(source, level))


def test_signed_pdf_is_reported_as_signature_risk(tmp_path):
    source = signature_pdf(tmp_path / "signed.pdf", signed=True)
    report = analyze_pdf(source, "2b")
    assert report.features["signed"] is True
    assert "digital_signature" in _codes(report)


def test_empty_signature_field_is_not_treated_as_signed(tmp_path):
    source = signature_pdf(tmp_path / "unsigned-field.pdf", signed=False)
    report = analyze_pdf(source, "2b")
    assert report.features["signed"] is False
    assert "digital_signature" not in _codes(report)


def test_type0_font_routes_away_from_unsafe_fast_embedding(tmp_path):
    source = type0_font_pdf(tmp_path / "type0.pdf")
    report = analyze_pdf(source, "2b")
    assert report.features["type0_fonts"] == 1
    assert report.features["fonts_unembedded"] == 1
    assert "type0_fonts" in _codes(report)
    assert _requires_full_rewrite(report) is True


def test_device_rgb_image_is_counted_but_object_level_repairable(tmp_path):
    source = device_rgb_image_pdf(tmp_path / "device-rgb.pdf")
    report = analyze_pdf(source, "2b")
    assert report.features["device_color_spaces"]["/DeviceRGB"] >= 1
    assert _requires_full_rewrite(report) is False


def test_device_cmyk_routes_to_full_color_rewrite(tmp_path):
    source = device_cmyk_image_pdf(tmp_path / "device-cmyk.pdf")
    report = analyze_pdf(source, "2b")
    assert report.features["device_color_spaces"]["/DeviceCMYK"] >= 1
    assert _requires_full_rewrite(report) is True


def test_direct_device_color_operator_routes_to_full_rewrite(tmp_path):
    source = direct_device_color_pdf(tmp_path / "direct-rgb.pdf", "rg")
    report = analyze_pdf(source, "2b")
    assert report.features["direct_device_color_operators"]["/DeviceRGB"] >= 1
    assert "direct_device_color" in _codes(report)
    assert _requires_full_rewrite(report) is True
