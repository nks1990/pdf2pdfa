"""Byte-accurate PDF COS tokenizer and direct-object parser."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import string
from typing import Callable

from .objects import PDFDict, PDFName, PDFObject, PDFRef


_WHITESPACE = b"\x00\x09\x0a\x0c\x0d\x20"
_DELIMITERS = b"()<>[]{}/%"
_HEX = set(b"0123456789abcdefABCDEF")
_NAME_SAFE = set(bytes(string.ascii_letters + string.digits + "-_.+", "ascii"))


class PDFSyntaxError(ValueError):
    pass


class PDFTokenizer:
    def __init__(self, data: bytes, position: int = 0, end: int | None = None) -> None:
        self.data = data
        self.position = position
        self.end = len(data) if end is None else min(end, len(data))

    def eof(self) -> bool:
        self.skip_space()
        return self.position >= self.end

    def skip_space(self) -> None:
        data = self.data
        while self.position < self.end:
            byte = data[self.position]
            if byte in _WHITESPACE:
                self.position += 1
                continue
            if byte == ord("%"):
                self.position += 1
                while self.position < self.end and data[self.position] not in (10, 13):
                    self.position += 1
                continue
            break

    def peek_keyword(self, keyword: bytes) -> bool:
        save = self.position
        try:
            token = self.read_regular_token()
            return token == keyword
        except PDFSyntaxError:
            return False
        finally:
            self.position = save

    def read_regular_token(self) -> bytes:
        self.skip_space()
        if self.position >= self.end:
            raise PDFSyntaxError("Unexpected end of PDF data")
        start = self.position
        byte = self.data[self.position]
        if byte in _DELIMITERS:
            if self.data.startswith(b"<<", self.position) or self.data.startswith(b">>", self.position):
                self.position += 2
                return self.data[start : self.position]
            self.position += 1
            return self.data[start : self.position]
        while self.position < self.end:
            byte = self.data[self.position]
            if byte in _WHITESPACE or byte in _DELIMITERS:
                break
            self.position += 1
        if self.position == start:
            raise PDFSyntaxError(f"Could not tokenize byte at offset {start}")
        return self.data[start : self.position]

    def expect(self, keyword: bytes) -> None:
        actual = self.read_regular_token()
        if actual != keyword:
            raise PDFSyntaxError(
                f"Expected {keyword!r} at offset {self.position - len(actual)}, got {actual!r}"
            )

    def parse_object(self) -> PDFObject:
        self.skip_space()
        if self.position >= self.end:
            raise PDFSyntaxError("Unexpected end while parsing object")
        if self.data.startswith(b"<<", self.position):
            return self._parse_dictionary()
        byte = self.data[self.position]
        if byte == ord("["):
            return self._parse_array()
        if byte == ord("("):
            return self._parse_literal_string()
        if byte == ord("<"):
            return self._parse_hex_string()
        if byte == ord("/"):
            return self._parse_name()

        token_start = self.position
        token = self.read_regular_token()
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        number = self._parse_number(token)
        if number is not None:
            # Detect an indirect reference without losing tokens on non-ref input.
            after_first = self.position
            try:
                second_token = self.read_regular_token()
                second = self._parse_number(second_token)
                if isinstance(number, int) and isinstance(second, int):
                    marker = self.read_regular_token()
                    if marker == b"R":
                        return PDFRef(number, second)
            except PDFSyntaxError:
                pass
            self.position = after_first
            return number
        raise PDFSyntaxError(f"Unexpected token {token!r} at offset {token_start}")

    @staticmethod
    def _parse_number(token: bytes) -> int | Decimal | None:
        if not token:
            return None
        try:
            text = token.decode("ascii")
        except UnicodeDecodeError:
            return None
        if any(ch not in "+-0123456789." for ch in text):
            return None
        if text in ("+", "-", ".", "+.", "-."):
            return None
        try:
            if "." not in text:
                return int(text, 10)
            return Decimal(text)
        except (ValueError, InvalidOperation):
            return None

    def _parse_array(self) -> list[PDFObject]:
        self.expect(b"[")
        values: list[PDFObject] = []
        while True:
            self.skip_space()
            if self.position >= self.end:
                raise PDFSyntaxError("Unterminated PDF array")
            if self.data[self.position] == ord("]"):
                self.position += 1
                return values
            values.append(self.parse_object())

    def _parse_dictionary(self) -> PDFDict:
        self.expect(b"<<")
        result = PDFDict()
        while True:
            self.skip_space()
            if self.data.startswith(b">>", self.position):
                self.position += 2
                return result
            if self.position >= self.end:
                raise PDFSyntaxError("Unterminated PDF dictionary")
            key = self.parse_object()
            if not isinstance(key, PDFName):
                raise PDFSyntaxError(
                    f"PDF dictionary key at offset {self.position} is not a name"
                )
            result[key.value] = self.parse_object()

    def _parse_name(self) -> PDFName:
        if self.data[self.position] != ord("/"):
            raise PDFSyntaxError("Internal name parser misuse")
        self.position += 1
        output = bytearray()
        while self.position < self.end:
            byte = self.data[self.position]
            if byte in _WHITESPACE or byte in _DELIMITERS:
                break
            if byte == ord("#") and self.position + 2 < self.end:
                pair = self.data[self.position + 1 : self.position + 3]
                if all(item in _HEX for item in pair):
                    output.append(int(pair, 16))
                    self.position += 3
                    continue
            output.append(byte)
            self.position += 1
        return PDFName(output.decode("latin-1"))

    def _parse_literal_string(self) -> bytes:
        if self.data[self.position] != ord("("):
            raise PDFSyntaxError("Internal literal-string parser misuse")
        self.position += 1
        depth = 1
        output = bytearray()
        while self.position < self.end:
            byte = self.data[self.position]
            self.position += 1
            if byte == ord("\\"):
                if self.position >= self.end:
                    break
                escaped = self.data[self.position]
                self.position += 1
                simple = {
                    ord("n"): 10,
                    ord("r"): 13,
                    ord("t"): 9,
                    ord("b"): 8,
                    ord("f"): 12,
                    ord("("): ord("("),
                    ord(")"): ord(")"),
                    ord("\\"): ord("\\"),
                }
                if escaped in simple:
                    output.append(simple[escaped])
                    continue
                if escaped == 13:
                    if self.position < self.end and self.data[self.position] == 10:
                        self.position += 1
                    continue
                if escaped == 10:
                    continue
                if 48 <= escaped <= 55:
                    digits = bytearray([escaped])
                    for _ in range(2):
                        if self.position < self.end and 48 <= self.data[self.position] <= 55:
                            digits.append(self.data[self.position])
                            self.position += 1
                        else:
                            break
                    output.append(int(digits, 8) & 0xFF)
                    continue
                output.append(escaped)
                continue
            if byte == ord("("):
                depth += 1
                output.append(byte)
                continue
            if byte == ord(")"):
                depth -= 1
                if depth == 0:
                    return bytes(output)
                output.append(byte)
                continue
            output.append(byte)
        raise PDFSyntaxError("Unterminated literal string")

    def _parse_hex_string(self) -> bytes:
        if self.data[self.position] != ord("<") or self.data.startswith(b"<<", self.position):
            raise PDFSyntaxError("Internal hex-string parser misuse")
        self.position += 1
        digits = bytearray()
        while self.position < self.end:
            byte = self.data[self.position]
            self.position += 1
            if byte == ord(">"):
                if len(digits) % 2:
                    digits.append(ord("0"))
                try:
                    return bytes.fromhex(digits.decode("ascii"))
                except ValueError as exc:
                    raise PDFSyntaxError("Invalid hexadecimal PDF string") from exc
            if byte in _WHITESPACE:
                continue
            if byte not in _HEX:
                raise PDFSyntaxError(
                    f"Invalid byte {byte:#x} in hexadecimal string at offset {self.position - 1}"
                )
            digits.append(byte)
        raise PDFSyntaxError("Unterminated hexadecimal string")


def encode_name(name: str | PDFName) -> bytes:
    value = name.value if isinstance(name, PDFName) else name.lstrip("/")
    raw = value.encode("latin-1")
    out = bytearray(b"/")
    for byte in raw:
        if byte in _NAME_SAFE:
            out.append(byte)
        else:
            out.extend(f"#{byte:02X}".encode("ascii"))
    return bytes(out)
