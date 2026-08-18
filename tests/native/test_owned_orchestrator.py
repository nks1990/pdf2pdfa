from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.orchestrator import (
    NativeConversionError,
    OwnedConversionOrchestrator,
    SignatureInvalidationError,
)
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.repair import NativeRepairEngine
from tests.native.security_fixture import make_aes256_encrypted_pdf


def _plain_pdf(*, javascript: bool = False, signature: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    content_ref = builder.add(PDFStream(PDFDict(), b"q 0 0 20 20 re f Q\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 200, 200],
                "Resources": PDFDict(),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    catalog = PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref})
    if javascript:
        catalog["OpenAction"] = PDFDict(
            {"S": PDFName("JavaScript"), "JS": b"app.alert('x')"}
        )
    if signature:
        sig = builder.add(
            PDFDict(
                {
                    "Type": PDFName("Sig"),
                    "Filter": PDFName("Adobe.PPKLite"),
                    "SubFilter": PDFName("adbe.pkcs7.detached"),
                    "Contents": b"signed-bytes",
                }
            )
        )
        field = builder.add(
            PDFDict({"FT": PDFName("Sig"), "T": b"Signature1", "V": sig})
        )
        catalog["AcroForm"] = PDFDict({"Fields": [field]})
    root_ref = builder.add(catalog)
    builder.set_root(root_ref)
    return builder.to_bytes()


def test_plain_pdf_converts_and_is_owned_validated(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedConversionOrchestrator().convert(
        _plain_pdf(), output, level="2b"
    )
    assert result.validation.compliant, result.validation.failures
    assert result.validation.engine == "pdf2pdfa-owned"
    assert result.fidelity is not None and result.fidelity.passed
    assert NativePDFAValidator().validate(output, "2b").compliant


def test_javascript_removal_preserves_page_semantics(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedConversionOrchestrator().convert(
        _plain_pdf(javascript=True), output, level="2b"
    )
    assert result.validation.compliant
    assert result.fidelity is not None and result.fidelity.passed
    assert b"JavaScript" not in output.read_bytes()


def test_signed_pdf_is_refused_before_rewrite(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    output.write_bytes(b"sentinel")
    try:
        OwnedConversionOrchestrator().convert(
            _plain_pdf(signature=True), output, level="2b"
        )
    except SignatureInvalidationError:
        pass
    else:
        raise AssertionError("signed PDF should have been refused")
    assert output.read_bytes() == b"sentinel"


def test_encrypted_aes256_pdf_is_decrypted_repaired_and_validated(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    result = OwnedConversionOrchestrator().convert(
        make_aes256_encrypted_pdf(revision=6),
        output,
        level="2b",
        password="user",
    )
    assert result.source_was_encrypted
    assert result.validation.compliant, result.validation.failures
    assert result.fidelity is not None and result.fidelity.passed
    assert b"/Encrypt" not in output.read_bytes()


def test_wrong_password_never_touches_destination(tmp_path: Path):
    output = tmp_path / "archive.pdf"
    output.write_bytes(b"sentinel")
    try:
        OwnedConversionOrchestrator().convert(
            make_aes256_encrypted_pdf(revision=6),
            output,
            level="2b",
            password="wrong",
        )
    except Exception:
        pass
    else:
        raise AssertionError("wrong password should fail")
    assert output.read_bytes() == b"sentinel"


def test_already_conforming_pdf_passes_through_byte_for_byte(tmp_path: Path):
    plain = tmp_path / "plain.pdf"
    normalized = tmp_path / "normalized.pdf"
    passthrough = tmp_path / "passthrough.pdf"
    plain.write_bytes(_plain_pdf())
    NativeRepairEngine().convert(plain, normalized, "2b")
    original = normalized.read_bytes()
    result = OwnedConversionOrchestrator().convert(normalized, passthrough, level="2b")
    assert result.source_was_already_compliant
    assert passthrough.read_bytes() == original
