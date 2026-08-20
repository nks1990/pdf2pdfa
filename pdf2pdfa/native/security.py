"""Owned PDF Standard Security Handler (password-based encryption).

Supports legacy revisions 2-4 (RC4/AES-128 crypt filters) and AES-256
revisions 5-6.  The implementation uses only Python's standard library plus
the owned AES primitive in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import stringprep
import unicodedata
from typing import BinaryIO

from .aes import AESError, cbc_decrypt, ecb_decrypt
from .document import PDFDocument, PDFParseError
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream, as_int


class PDFSecurityError(RuntimeError):
    pass


class UnsupportedSecurityHandlerError(PDFSecurityError):
    pass


class InvalidPasswordError(PDFSecurityError):
    pass


_PASSWORD_PADDING = bytes.fromhex(
    "28bf4e5e4e758a4164004e56fffa0108"
    "2e2e00b6d0683e802f0ca9fe6453697a"
)


def rc4(key: bytes, data: bytes) -> bytes:
    if not key:
        raise ValueError("RC4 key cannot be empty")
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        k = state[(state[i] + state[j]) & 0xFF]
        out.append(byte ^ k)
    return bytes(out)


def _xor_key(key: bytes, value: int) -> bytes:
    return bytes(byte ^ value for byte in key)


def _legacy_password_bytes(password: str | bytes) -> bytes:
    if isinstance(password, bytes):
        return password
    try:
        return password.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise InvalidPasswordError(
            "legacy PDF encryption passwords must be representable as single-byte text"
        ) from exc


def _pad_legacy(password: str | bytes) -> bytes:
    raw = _legacy_password_bytes(password)
    return (raw + _PASSWORD_PADDING)[:32]


def _saslprep(text: str) -> str:
    mapped = []
    for character in text:
        if stringprep.in_table_b1(character):
            continue
        if stringprep.in_table_c12(character):
            mapped.append(" ")
        else:
            mapped.append(character)
    prepared = unicodedata.normalize("NFKC", "".join(mapped))
    prohibited = (
        stringprep.in_table_c12,
        stringprep.in_table_c21,
        stringprep.in_table_c22,
        stringprep.in_table_c3,
        stringprep.in_table_c4,
        stringprep.in_table_c5,
        stringprep.in_table_c6,
        stringprep.in_table_c7,
        stringprep.in_table_c8,
        stringprep.in_table_c9,
    )
    for character in prepared:
        if any(check(character) for check in prohibited):
            raise InvalidPasswordError("password contains characters prohibited by SASLprep")
    has_randal = any(stringprep.in_table_d1(character) for character in prepared)
    has_lcat = any(stringprep.in_table_d2(character) for character in prepared)
    if has_randal:
        if has_lcat or not (
            stringprep.in_table_d1(prepared[0]) and stringprep.in_table_d1(prepared[-1])
        ):
            raise InvalidPasswordError("password violates SASLprep bidirectional rules")
    return prepared


def _modern_password_bytes(password: str | bytes, revision: int) -> bytes:
    if isinstance(password, bytes):
        raw = password
    else:
        text = _saslprep(password) if revision >= 6 else password
        raw = text.encode("utf-8")
    return raw[:127]


def _bytes(value: PDFObject | None, label: str) -> bytes:
    if not isinstance(value, bytes):
        raise PDFSecurityError(f"encryption dictionary {label} shall be a byte string")
    return value


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    if isinstance(value, PDFRef):
        value = PDFDocument.get(doc, value)
    if not isinstance(value, PDFDict):
        raise PDFSecurityError(f"{label} shall be a dictionary")
    return value


def _id0(doc: PDFDocument) -> bytes:
    value = doc.trailer.get("ID")
    if not isinstance(value, list) or not value or not isinstance(value[0], bytes):
        raise PDFSecurityError("encrypted legacy PDF requires trailer /ID byte strings")
    return value[0]


def _legacy_key_length(encrypt: PDFDict, revision: int) -> int:
    if revision == 2:
        return 5
    bits = as_int(encrypt.get("Length"), 40)
    if bits % 8 or bits < 40 or bits > 128:
        raise PDFSecurityError(f"invalid legacy encryption key length {bits} bits")
    return bits // 8


def _legacy_file_key(
    password: str | bytes,
    *,
    encrypt: PDFDict,
    revision: int,
    identifier: bytes,
) -> bytes:
    owner = _bytes(encrypt.get("O"), "/O")
    permissions = as_int(encrypt.get("P"), 0)
    length = _legacy_key_length(encrypt, revision)
    material = bytearray(_pad_legacy(password))
    material.extend(owner)
    material.extend((permissions & 0xFFFFFFFF).to_bytes(4, "little"))
    material.extend(identifier)
    encrypt_metadata = encrypt.get("EncryptMetadata")
    if revision >= 4 and encrypt_metadata is False:
        material.extend(b"\xff\xff\xff\xff")
    digest = hashlib.md5(bytes(material)).digest()
    if revision >= 3:
        for _ in range(50):
            digest = hashlib.md5(digest[:length]).digest()
    return digest[:length]


def _legacy_user_value(key: bytes, revision: int, identifier: bytes) -> bytes:
    if revision == 2:
        return rc4(key, _PASSWORD_PADDING)
    digest = hashlib.md5(_PASSWORD_PADDING + identifier).digest()
    value = rc4(key, digest)
    for index in range(1, 20):
        value = rc4(_xor_key(key, index), value)
    return value + bytes(16)


def _legacy_validate_user(
    password: str | bytes,
    *,
    encrypt: PDFDict,
    revision: int,
    identifier: bytes,
) -> bytes | None:
    key = _legacy_file_key(
        password,
        encrypt=encrypt,
        revision=revision,
        identifier=identifier,
    )
    expected = _bytes(encrypt.get("U"), "/U")
    actual = _legacy_user_value(key, revision, identifier)
    if revision == 2:
        return key if actual == expected else None
    return key if actual[:16] == expected[:16] else None


def _legacy_owner_user_password(
    owner_password: str | bytes,
    *,
    encrypt: PDFDict,
    revision: int,
) -> bytes:
    length = _legacy_key_length(encrypt, revision)
    digest = hashlib.md5(_pad_legacy(owner_password)).digest()
    if revision >= 3:
        for _ in range(50):
            digest = hashlib.md5(digest).digest()
    key = digest[:length]
    value = _bytes(encrypt.get("O"), "/O")
    if revision == 2:
        return rc4(key, value)
    for index in range(19, -1, -1):
        value = rc4(_xor_key(key, index), value)
    return value


def _r6_hash(password: bytes, salt: bytes, user_key: bytes = b"") -> bytes:
    k = hashlib.sha256(password + salt + user_key).digest()
    round_index = 0
    last_byte = 0
    while round_index < 64 or last_byte > round_index - 32:
        base = password + k + user_key
        k1 = base * 64
        encrypted = __import__("pdf2pdfa.native.aes", fromlist=["cbc_encrypt"]).cbc_encrypt(
            k[:16], k1, k[16:32], pad=False
        )
        selector = sum(encrypted[:16]) % 3
        if selector == 0:
            k = hashlib.sha256(encrypted).digest()
        elif selector == 1:
            k = hashlib.sha384(encrypted).digest()
        else:
            k = hashlib.sha512(encrypted).digest()
        last_byte = encrypted[-1]
        round_index += 1
    return k[:32]


def _modern_hash(password: bytes, salt: bytes, user_key: bytes, revision: int) -> bytes:
    if revision >= 6:
        return _r6_hash(password, salt, user_key)
    return hashlib.sha256(password + salt + user_key).digest()


@dataclass(frozen=True, slots=True)
class CryptFilter:
    name: str
    method: str  # Identity, V2, AESV2, AESV3


@dataclass(frozen=True, slots=True)
class StandardSecurityHandler:
    encrypt_ref: PDFRef
    revision: int
    version: int
    file_key: bytes
    permissions: int
    encrypt_metadata: bool
    stream_filter: CryptFilter
    string_filter: CryptFilter
    embedded_file_filter: CryptFilter

    @classmethod
    def authorize(
        cls,
        doc: PDFDocument,
        password: str | bytes,
    ) -> "StandardSecurityHandler":
        encrypt_value = doc.trailer.get("Encrypt")
        if not isinstance(encrypt_value, PDFRef):
            raise UnsupportedSecurityHandlerError(
                "owned security handler currently requires an indirect /Encrypt dictionary"
            )
        encrypt = _dict(doc, encrypt_value, "trailer /Encrypt")
        if _name(encrypt.get("Filter")) not in ("", "Standard"):
            raise UnsupportedSecurityHandlerError(
                f"security handler /{_name(encrypt.get('Filter'))} is not the Standard password handler"
            )
        revision = as_int(encrypt.get("R"), 0)
        version = as_int(encrypt.get("V"), 0)
        permissions = as_int(encrypt.get("P"), 0)
        encrypt_metadata = encrypt.get("EncryptMetadata") is not False

        if revision in (2, 3, 4):
            identifier = _id0(doc)
            key = _legacy_validate_user(
                password,
                encrypt=encrypt,
                revision=revision,
                identifier=identifier,
            )
            if key is None:
                recovered = _legacy_owner_user_password(
                    password,
                    encrypt=encrypt,
                    revision=revision,
                )
                key = _legacy_validate_user(
                    recovered,
                    encrypt=encrypt,
                    revision=revision,
                    identifier=identifier,
                )
            if key is None:
                raise InvalidPasswordError("password does not authorize this encrypted PDF")
        elif revision in (5, 6):
            key = cls._authorize_modern(encrypt, password, revision, permissions, encrypt_metadata)
        else:
            raise UnsupportedSecurityHandlerError(
                f"unsupported Standard security handler revision {revision}"
            )

        stream_filter = cls._default_filter(doc, encrypt, version, "StmF", revision)
        string_filter = cls._default_filter(doc, encrypt, version, "StrF", revision)
        embedded_filter = cls._default_filter(doc, encrypt, version, "EFF", revision, fallback=stream_filter)
        return cls(
            encrypt_ref=encrypt_value,
            revision=revision,
            version=version,
            file_key=key,
            permissions=permissions,
            encrypt_metadata=encrypt_metadata,
            stream_filter=stream_filter,
            string_filter=string_filter,
            embedded_file_filter=embedded_filter,
        )

    @classmethod
    def _authorize_modern(
        cls,
        encrypt: PDFDict,
        password: str | bytes,
        revision: int,
        permissions: int,
        encrypt_metadata: bool,
    ) -> bytes:
        password_bytes = _modern_password_bytes(password, revision)
        user = _bytes(encrypt.get("U"), "/U")
        owner = _bytes(encrypt.get("O"), "/O")
        ue = _bytes(encrypt.get("UE"), "/UE")
        oe = _bytes(encrypt.get("OE"), "/OE")
        if len(user) != 48 or len(owner) != 48 or len(ue) != 32 or len(oe) != 32:
            raise PDFSecurityError("AES-256 Standard security entries have invalid lengths")

        user_hash = _modern_hash(password_bytes, user[32:40], b"", revision)
        if user_hash == user[:32]:
            intermediate = _modern_hash(password_bytes, user[40:48], b"", revision)
            file_key = cbc_decrypt(intermediate, ue, bytes(16), unpad=False)
        else:
            owner_hash = _modern_hash(password_bytes, owner[32:40], user, revision)
            if owner_hash != owner[:32]:
                raise InvalidPasswordError("password does not authorize this encrypted PDF")
            intermediate = _modern_hash(password_bytes, owner[40:48], user, revision)
            file_key = cbc_decrypt(intermediate, oe, bytes(16), unpad=False)
        if len(file_key) != 32:
            raise PDFSecurityError("AES-256 file key has invalid length")

        perms = encrypt.get("Perms")
        if isinstance(perms, bytes) and len(perms) == 16:
            plain = ecb_decrypt(file_key, perms)
            expected_permissions = permissions & 0xFFFFFFFF
            if int.from_bytes(plain[0:4], "little") != expected_permissions:
                raise PDFSecurityError("AES-256 permissions block does not match /P")
            if plain[4:8] != b"\xff\xff\xff\xff" or plain[9:12] != b"adb":
                raise PDFSecurityError("AES-256 permissions block is malformed")
            expected_meta = b"T" if encrypt_metadata else b"F"
            if plain[8:9] != expected_meta:
                raise PDFSecurityError("AES-256 permissions EncryptMetadata flag mismatch")
        return file_key

    @classmethod
    def _default_filter(
        cls,
        doc: PDFDocument,
        encrypt: PDFDict,
        version: int,
        key: str,
        revision: int,
        *,
        fallback: CryptFilter | None = None,
    ) -> CryptFilter:
        if version < 4:
            return CryptFilter("StdImplicit", "V2")
        name = _name(encrypt.get(key))
        if not name:
            return fallback or CryptFilter("Identity", "Identity")
        return cls._named_filter(doc, encrypt, name, revision)

    @classmethod
    def _named_filter(
        cls,
        doc: PDFDocument,
        encrypt: PDFDict,
        name: str,
        revision: int,
    ) -> CryptFilter:
        if name == "Identity":
            return CryptFilter(name, "Identity")
        cf = _dict(doc, encrypt.get("CF"), "/Encrypt/CF")
        entry = _dict(doc, cf.get(name), f"/Encrypt/CF/{name}")
        method = _name(entry.get("CFM")) or "None"
        if method == "None":
            method = "Identity"
        allowed = {"Identity", "V2", "AESV2", "AESV3"}
        if method not in allowed:
            raise UnsupportedSecurityHandlerError(f"unsupported crypt filter method /{method}")
        if revision <= 4 and method == "AESV3":
            raise PDFSecurityError("AESV3 is inconsistent with legacy Standard security revision")
        if revision >= 5 and method not in ("AESV3", "Identity"):
            raise PDFSecurityError("AES-256 security handler requires AESV3 or Identity filters")
        return CryptFilter(name, method)

    def _object_key(self, ref: PDFRef, method: str) -> bytes:
        if self.revision >= 5:
            return self.file_key
        material = bytearray(self.file_key)
        material.extend(ref.object_number.to_bytes(3, "little", signed=False))
        material.extend(ref.generation.to_bytes(2, "little", signed=False))
        if method == "AESV2":
            material.extend(b"sAlT")
        digest = hashlib.md5(bytes(material)).digest()
        return digest[: min(len(self.file_key) + 5, 16)]

    def decrypt_bytes(self, ref: PDFRef, data: bytes, crypt_filter: CryptFilter) -> bytes:
        method = crypt_filter.method
        if method == "Identity":
            return data
        key = self._object_key(ref, method)
        if method == "V2":
            return rc4(key, data)
        if method in ("AESV2", "AESV3"):
            if len(data) < 16:
                raise PDFSecurityError("AES-encrypted PDF object is shorter than its IV")
            iv, ciphertext = data[:16], data[16:]
            if not ciphertext or len(ciphertext) % 16:
                raise PDFSecurityError("AES-encrypted PDF object has invalid ciphertext length")
            try:
                return cbc_decrypt(key, ciphertext, iv, unpad=True)
            except AESError as exc:
                raise PDFSecurityError(f"invalid AES-encrypted PDF object: {exc}") from exc
        raise UnsupportedSecurityHandlerError(f"unsupported crypt filter method {method}")

    def _explicit_stream_filter(
        self,
        doc: PDFDocument,
        stream: PDFStream,
    ) -> CryptFilter | None:
        filters = stream.get("Filter")
        if isinstance(filters, PDFName):
            names = [filters]
        elif isinstance(filters, list):
            names = [item for item in filters if isinstance(item, PDFName)]
        else:
            return None
        try:
            index = next(i for i, item in enumerate(names) if item.value == "Crypt")
        except StopIteration:
            return None
        decode_parms = stream.get("DecodeParms")
        params: PDFObject | None = None
        if isinstance(decode_parms, list) and index < len(decode_parms):
            params = decode_parms[index]
        elif isinstance(decode_parms, PDFDict) and index == 0:
            params = decode_parms
        if isinstance(params, PDFRef):
            params = doc.get(params)
        filter_name = "Identity"
        if isinstance(params, PDFDict):
            filter_name = _name(params.get("Name")) or "Identity"
        # Locate the Encrypt dictionary directly from the trailer. It is exempt
        # from encryption and may be read through the base class.
        encrypt = _dict(doc, self.encrypt_ref, "trailer /Encrypt")
        return self._named_filter(doc, encrypt, filter_name, self.revision)

    def _remove_crypt_filter(self, stream: PDFStream) -> None:
        filters = stream.get("Filter")
        parms = stream.get("DecodeParms")
        if isinstance(filters, PDFName):
            if filters.value == "Crypt":
                stream.dictionary.pop("Filter", None)
                stream.dictionary.pop("DecodeParms", None)
            return
        if not isinstance(filters, list):
            return
        new_filters: list[PDFObject] = []
        new_parms: list[PDFObject] = []
        parms_list = parms if isinstance(parms, list) else []
        for index, item in enumerate(filters):
            if isinstance(item, PDFName) and item.value == "Crypt":
                continue
            new_filters.append(item)
            if parms_list:
                new_parms.append(parms_list[index] if index < len(parms_list) else None)
        if not new_filters:
            stream.dictionary.pop("Filter", None)
            stream.dictionary.pop("DecodeParms", None)
        elif len(new_filters) == 1:
            stream.dictionary["Filter"] = new_filters[0]
            if new_parms and new_parms[0] is not None:
                stream.dictionary["DecodeParms"] = new_parms[0]
            else:
                stream.dictionary.pop("DecodeParms", None)
        else:
            stream.dictionary["Filter"] = new_filters
            if new_parms:
                stream.dictionary["DecodeParms"] = new_parms

    def decrypt_object(self, doc: PDFDocument, ref: PDFRef, value: PDFObject) -> PDFObject:
        if ref == self.encrypt_ref:
            return value
        return self._decrypt_value(doc, ref, value, key=None, parent_type="")

    def _decrypt_value(
        self,
        doc: PDFDocument,
        ref: PDFRef,
        value: PDFObject,
        *,
        key: str | None,
        parent_type: str,
    ) -> PDFObject:
        if isinstance(value, bytes):
            if parent_type == "Sig" and key == "Contents":
                return value
            return self.decrypt_bytes(ref, value, self.string_filter)
        if isinstance(value, PDFStream):
            stream_type = _name(value.get("Type"))
            if stream_type != "XRef":
                explicit = self._explicit_stream_filter(doc, value)
                crypt_filter = explicit
                if crypt_filter is None:
                    if stream_type == "EmbeddedFile":
                        crypt_filter = self.embedded_file_filter
                    elif stream_type == "Metadata" and not self.encrypt_metadata:
                        crypt_filter = CryptFilter("Identity", "Identity")
                    else:
                        crypt_filter = self.stream_filter
                value.data = self.decrypt_bytes(ref, value.data, crypt_filter)
                if explicit is not None:
                    self._remove_crypt_filter(value)
                value.dictionary["Length"] = len(value.data)
            self._decrypt_value(
                doc,
                ref,
                value.dictionary,
                key=None,
                parent_type=stream_type,
            )
            return value
        if isinstance(value, PDFDict):
            current_type = _name(value.get("Type")) or parent_type
            for child_key, child in list(value.items()):
                value[child_key] = self._decrypt_value(
                    doc,
                    ref,
                    child,
                    key=child_key,
                    parent_type=current_type,
                )
            return value
        if isinstance(value, list):
            for index, child in enumerate(list(value)):
                value[index] = self._decrypt_value(
                    doc,
                    ref,
                    child,
                    key=key,
                    parent_type=parent_type,
                )
            return value
        return value


class SecurePDFDocument(PDFDocument):
    """PDFDocument that decrypts Standard-handler objects lazily on access."""

    def __init__(self, data: bytes, *, repair: bool = True) -> None:
        super().__init__(data, repair=repair)
        self.security_handler: StandardSecurityHandler | None = None
        self._security_decrypted: set[PDFRef] = set()

    @classmethod
    def open_secure(
        cls,
        source: str | Path | bytes | bytearray | BinaryIO,
        password: str | bytes,
        *,
        repair: bool = True,
    ) -> "SecurePDFDocument":
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        elif isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            data = source.read()
        doc = cls(data, repair=repair)
        if "Encrypt" not in doc.trailer:
            return doc
        doc.security_handler = StandardSecurityHandler.authorize(doc, password)
        return doc

    def get(self, ref: PDFRef) -> PDFObject:
        value = super().get(ref)
        handler = self.security_handler
        if handler is None or ref in self._security_decrypted or ref == handler.encrypt_ref:
            return value
        entry = self.xref.get(ref.object_number)
        if entry is not None and entry.kind == 2:
            # Object-stream members were decrypted as part of the ObjStm bytes.
            self._security_decrypted.add(ref)
            return value
        handler.decrypt_object(self, ref, value)
        self._security_decrypted.add(ref)
        self._cache[ref] = value
        return value

    @property
    def is_encrypted_source(self) -> bool:
        return "Encrypt" in self.trailer

    def remove_encryption_for_write(self) -> None:
        """Materialize reachable plaintext objects and remove encryption trailer state."""
        if self.security_handler is None:
            self.trailer.pop("Encrypt", None)
            return
        # Reachability traversal forces lazy decryption of every object that the
        # output writer can retain, including encrypted object streams.
        for ref in list(self.reachable_refs()):
            try:
                self.get(ref)
            except KeyError:
                continue
        self.trailer.pop("Encrypt", None)
        self.security_handler = None
