from __future__ import annotations

from pdf2pdfa.native.objects import PDFDict, PDFRef, PDFStream
from pdf2pdfa.native.security import InvalidPasswordError, SecurePDFDocument
from pdf2pdfa.native.structure import decoded_stream_bytes, walk_pages
from pdf2pdfa.native.writer import PDFWriter
from tests.native.security_fixture import make_aes256_encrypted_pdf, make_legacy_encrypted_pdf


def _assert_decrypts(pdf: bytes, password: str) -> SecurePDFDocument:
    doc = SecurePDFDocument.open_secure(pdf, password, repair=False)
    page = next(walk_pages(doc))
    contents = doc.get(page.dictionary["Contents"])
    assert isinstance(contents, PDFStream)
    assert decoded_stream_bytes(doc, contents).startswith(b"q")
    info_ref = doc.trailer["Info"]
    assert isinstance(info_ref, PDFRef)
    info = doc.get(info_ref)
    assert isinstance(info, PDFDict)
    assert b"title" in info["Title"].lower()
    return doc


def test_revision2_rc4_user_and_owner_passwords():
    pdf = make_legacy_encrypted_pdf(revision=2)
    _assert_decrypts(pdf, "user")
    _assert_decrypts(pdf, "owner")


def test_revision4_aes128_user_and_owner_passwords():
    pdf = make_legacy_encrypted_pdf(revision=4)
    _assert_decrypts(pdf, "user")
    _assert_decrypts(pdf, "owner")


def test_revision5_aes256_user_and_owner_passwords():
    pdf = make_aes256_encrypted_pdf(revision=5)
    _assert_decrypts(pdf, "user")
    _assert_decrypts(pdf, "owner")


def test_revision6_aes256_hardened_hash_user_and_owner_passwords():
    pdf = make_aes256_encrypted_pdf(revision=6)
    _assert_decrypts(pdf, "user")
    _assert_decrypts(pdf, "owner")


def test_wrong_password_is_rejected():
    pdf = make_legacy_encrypted_pdf(revision=4)
    try:
        SecurePDFDocument.open_secure(pdf, "wrong", repair=False)
    except InvalidPasswordError:
        pass
    else:
        raise AssertionError("wrong password unexpectedly authorized")


def test_decrypted_document_can_be_rewritten_without_encrypt_dictionary(tmp_path):
    pdf = make_aes256_encrypted_pdf(revision=6)
    doc = _assert_decrypts(pdf, "user")
    doc.remove_encryption_for_write()
    output = tmp_path / "decrypted.pdf"
    PDFWriter(doc, version="1.7").write(output)
    raw = output.read_bytes()
    assert b"/Encrypt" not in raw
    assert b"AES256 title" in raw

    reopened = SecurePDFDocument.open_secure(raw, "", repair=False)
    assert not reopened.is_encrypted_source
    page = next(walk_pages(reopened))
    stream = reopened.get(page.dictionary["Contents"])
    assert isinstance(stream, PDFStream)
    assert decoded_stream_bytes(reopened, stream) == b"q Q\n"
