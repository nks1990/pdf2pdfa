from __future__ import annotations

import pikepdf
from pikepdf import Dictionary, Name

from pdf2pdfa.preflight import analyze_pdf
from pdf2pdfa.profiles import get_policy


def test_profile_differences():
    assert get_policy("1b").allow_transparency is False
    assert get_policy("2b").allow_transparency is True
    assert get_policy("2b").allow_embedded_files is False
    assert get_policy("3b").allow_embedded_files is True


def test_preflight_clean_blank_pdf(tmp_path):
    path = tmp_path / "blank.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.save(path)

    report = analyze_pdf(path, "1b")
    assert report.level == "1b"
    assert report.features["encrypted"] is False
    assert report.features["signed"] is False
    assert report.features["javascript"] is False
    assert report.features["attachments"] is False


def test_pdfa1_transparency_is_error(tmp_path):
    path = tmp_path / "transparent.pdf"
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    gs = Dictionary({"/Type": Name("/ExtGState"), "/ca": 0.5})
    page.Resources["/ExtGState"] = Dictionary({"/GS0": gs})
    pdf.save(path)

    report_1b = analyze_pdf(path, "1b")
    report_2b = analyze_pdf(path, "2b")
    assert any(issue.code == "transparency" for issue in report_1b.errors)
    assert not any(issue.code == "transparency" for issue in report_2b.errors)
