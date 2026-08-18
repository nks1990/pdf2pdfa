from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdf2pdfa.preflight import analyze_pdf
from pdf2pdfa.security import (
    InputSecurityError,
    PasswordRequiredError,
    decrypt_to_file,
    read_password_file,
    validate_input_file,
)


def _encrypted_pdf(path: Path, password: str = "secret") -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.save(
        path,
        encryption=pikepdf.Encryption(owner="owner-secret", user=password, R=6),
    )
    pdf.close()
    return path


def test_encrypted_preflight_requires_password(tmp_path):
    source = _encrypted_pdf(tmp_path / "encrypted.pdf")
    with pytest.raises(PasswordRequiredError):
        analyze_pdf(source, "1b")


def test_encrypted_preflight_reports_encryption_with_password(tmp_path):
    source = _encrypted_pdf(tmp_path / "encrypted.pdf")
    report = analyze_pdf(source, "1b", password="secret")
    assert report.features["encrypted"] is True
    assert any(issue.code == "encryption" for issue in report.errors)


def test_decrypt_to_file_removes_encryption(tmp_path):
    source = _encrypted_pdf(tmp_path / "encrypted.pdf")
    output = decrypt_to_file(source, tmp_path / "decrypted.pdf", password="secret")
    with pikepdf.Pdf.open(output) as pdf:
        assert pdf.is_encrypted is False


def test_password_file_removes_only_final_newline(tmp_path):
    path = tmp_path / "password.txt"
    path.write_text(" secret value \n", encoding="utf-8")
    assert read_password_file(path) == " secret value "


def test_input_size_limit_is_enforced(tmp_path):
    path = tmp_path / "large.pdf"
    path.write_bytes(b"x" * 128)
    with pytest.raises(InputSecurityError, match="exceeding"):
        validate_input_file(path, max_bytes=64)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    with pytest.raises(InputSecurityError, match="empty"):
        validate_input_file(path)
