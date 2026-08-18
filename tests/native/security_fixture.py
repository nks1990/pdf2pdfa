from __future__ import annotations

import hashlib

from pdf2pdfa.native.aes import cbc_encrypt, ecb_encrypt
from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFRef, PDFStream
from pdf2pdfa.native.security import (
    _legacy_file_key,
    _legacy_user_value,
    _modern_hash,
    _modern_password_bytes,
    _pad_legacy,
    rc4,
)


def _xor_key(key: bytes, value: int) -> bytes:
    return bytes(byte ^ value for byte in key)


def _legacy_owner_entry(user: bytes, owner: bytes, *, revision: int, key_length: int) -> bytes:
    digest = hashlib.md5(_pad_legacy(owner or user)).digest()
    if revision >= 3:
        for _ in range(50):
            digest = hashlib.md5(digest).digest()
    key = digest[:key_length]
    value = _pad_legacy(user)
    value = rc4(key, value)
    if revision >= 3:
        for index in range(1, 20):
            value = rc4(_xor_key(key, index), value)
    return value


def _object_key(file_key: bytes, ref: PDFRef, *, aes: bool) -> bytes:
    material = bytearray(file_key)
    material.extend(ref.object_number.to_bytes(3, "little"))
    material.extend(ref.generation.to_bytes(2, "little"))
    if aes:
        material.extend(b"sAlT")
    return hashlib.md5(bytes(material)).digest()[: min(len(file_key) + 5, 16)]


def _encrypt_legacy_object(file_key: bytes, ref: PDFRef, data: bytes, *, aes: bool) -> bytes:
    key = _object_key(file_key, ref, aes=aes)
    if not aes:
        return rc4(key, data)
    iv = bytes.fromhex("00112233445566778899aabbccddeeff")
    return iv + cbc_encrypt(key, data, iv, pad=True)


def make_legacy_encrypted_pdf(
    *,
    revision: int,
    user: bytes = b"user",
    owner: bytes = b"owner",
) -> bytes:
    if revision not in (2, 4):
        raise ValueError("fixture supports revisions 2 and 4")
    builder = PDFBuilder(version="1.4" if revision == 2 else "1.6")
    content_ref = builder.add(PDFStream(PDFDict(), b"placeholder"))
    info_ref = builder.add(PDFDict({"Title": b"placeholder"}))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root_ref = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root_ref)
    builder.set_info(info_ref)

    identifier = bytes.fromhex("00112233445566778899aabbccddeeff")
    permissions = -4
    key_length = 5 if revision == 2 else 16
    owner_entry = _legacy_owner_entry(
        user,
        owner,
        revision=revision,
        key_length=key_length,
    )
    encrypt = PDFDict(
        {
            "Filter": PDFName("Standard"),
            "V": 1 if revision == 2 else 4,
            "R": revision,
            "Length": key_length * 8,
            "O": owner_entry,
            "U": bytes(32),
            "P": permissions,
        }
    )
    if revision == 4:
        encrypt["CF"] = PDFDict(
            {
                "StdCF": PDFDict(
                    {
                        "CFM": PDFName("AESV2"),
                        "Length": 16,
                        "AuthEvent": PDFName("DocOpen"),
                    }
                )
            }
        )
        encrypt["StmF"] = PDFName("StdCF")
        encrypt["StrF"] = PDFName("StdCF")
        encrypt["EncryptMetadata"] = True
    file_key = _legacy_file_key(
        user,
        encrypt=encrypt,
        revision=revision,
        identifier=identifier,
    )
    encrypt["U"] = _legacy_user_value(file_key, revision, identifier)
    encrypt_ref = builder.add(encrypt)
    builder.set_trailer("Encrypt", encrypt_ref)
    builder.set_trailer("ID", [identifier, identifier])

    aes = revision == 4
    content = builder.objects[content_ref]
    assert isinstance(content, PDFStream)
    content.data = _encrypt_legacy_object(
        file_key,
        content_ref,
        b"q 0 0 10 10 re f Q\n",
        aes=aes,
    )
    content.dictionary["Length"] = len(content.data)
    info = builder.objects[info_ref]
    assert isinstance(info, PDFDict)
    info["Title"] = _encrypt_legacy_object(
        file_key,
        info_ref,
        b"Encrypted title",
        aes=aes,
    )
    return builder.to_bytes()


def make_aes256_encrypted_pdf(
    *,
    revision: int = 6,
    user: str = "user",
    owner: str = "owner",
) -> bytes:
    if revision not in (5, 6):
        raise ValueError("AES-256 fixture requires revision 5 or 6")
    builder = PDFBuilder(version="1.7")
    content_ref = builder.add(PDFStream(PDFDict(), b"placeholder"))
    info_ref = builder.add(PDFDict({"Title": b"placeholder"}))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict(),
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root_ref = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root_ref)
    builder.set_info(info_ref)

    user_bytes = _modern_password_bytes(user, revision)
    owner_bytes = _modern_password_bytes(owner, revision)
    file_key = bytes(range(32))
    u_validation = b"UvSalt01"
    u_key = b"UkSalt01"
    user_entry = _modern_hash(user_bytes, u_validation, b"", revision) + u_validation + u_key
    ue_key = _modern_hash(user_bytes, u_key, b"", revision)
    ue = cbc_encrypt(ue_key, file_key, bytes(16), pad=False)

    o_validation = b"OvSalt01"
    o_key = b"OkSalt01"
    owner_entry = _modern_hash(owner_bytes, o_validation, user_entry, revision) + o_validation + o_key
    oe_key = _modern_hash(owner_bytes, o_key, user_entry, revision)
    oe = cbc_encrypt(oe_key, file_key, bytes(16), pad=False)
    permissions = -4
    perms_plain = (
        (permissions & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\xff\xff\xff\xff"
        + b"T"
        + b"adb"
        + b"TEST"
    )
    perms = ecb_encrypt(file_key, perms_plain)
    encrypt = PDFDict(
        {
            "Filter": PDFName("Standard"),
            "V": 5,
            "R": revision,
            "Length": 256,
            "O": owner_entry,
            "U": user_entry,
            "OE": oe,
            "UE": ue,
            "Perms": perms,
            "P": permissions,
            "EncryptMetadata": True,
            "CF": PDFDict(
                {
                    "StdCF": PDFDict(
                        {
                            "CFM": PDFName("AESV3"),
                            "Length": 32,
                            "AuthEvent": PDFName("DocOpen"),
                        }
                    )
                }
            ),
            "StmF": PDFName("StdCF"),
            "StrF": PDFName("StdCF"),
        }
    )
    encrypt_ref = builder.add(encrypt)
    builder.set_trailer("Encrypt", encrypt_ref)
    identifier = bytes.fromhex("102132435465768798a9bacbdcedfe0f")
    builder.set_trailer("ID", [identifier, identifier])

    iv = bytes.fromhex("ffeeddccbbaa99887766554433221100")
    content = builder.objects[content_ref]
    assert isinstance(content, PDFStream)
    content.data = iv + cbc_encrypt(file_key, b"q Q\n", iv, pad=True)
    content.dictionary["Length"] = len(content.data)
    info = builder.objects[info_ref]
    assert isinstance(info, PDFDict)
    info["Title"] = iv + cbc_encrypt(file_key, b"AES256 title", iv, pad=True)
    return builder.to_bytes()
