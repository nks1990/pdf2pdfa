"""Owned parser for the CMap subset required to map Type0 codes to CIDs."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


class CMapError(ValueError):
    pass


_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")


def _hex(token: bytes) -> bytes:
    match = _HEX.fullmatch(token.strip())
    if not match:
        raise CMapError(f"expected CMap hex string, got {token!r}")
    digits = match.group(1)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


def _tokens(data: bytes) -> list[bytes]:
    # CMap syntax is PostScript-like. For CID mapping we only need names,
    # integers, arrays and hex strings; literal strings/procedures can be skipped.
    data = re.sub(rb"%[^\r\n]*", b" ", data)
    pattern = re.compile(rb"<[^>]*>|\[|\]|/[A-Za-z0-9_.+-]+|-?\d+|[A-Za-z][A-Za-z0-9]*")
    return pattern.findall(data)


@dataclass(frozen=True, slots=True)
class CodeSpace:
    low: int
    high: int
    length: int

    def accepts(self, raw: bytes) -> bool:
        return len(raw) == self.length and self.low <= int.from_bytes(raw, "big") <= self.high


@dataclass(frozen=True, slots=True)
class CIDRange:
    start: int
    end: int
    code_length: int
    cid_start: int

    def lookup(self, raw: bytes) -> int | None:
        if len(raw) != self.code_length:
            return None
        code = int.from_bytes(raw, "big")
        if self.start <= code <= self.end:
            return self.cid_start + (code - self.start)
        return None


class CIDCMap:
    def __init__(
        self,
        *,
        codespaces: Iterable[CodeSpace],
        cid_chars: dict[bytes, int] | None = None,
        cid_ranges: Iterable[CIDRange] = (),
        vertical: bool = False,
    ) -> None:
        self.codespaces = tuple(codespaces)
        if not self.codespaces:
            raise CMapError("CMap has no codespace ranges")
        self.cid_chars = dict(cid_chars or {})
        self.cid_ranges = tuple(cid_ranges)
        self.vertical = vertical
        self._lengths = tuple(sorted({space.length for space in self.codespaces}, reverse=True))

    @classmethod
    def identity(cls, *, vertical: bool = False) -> "CIDCMap":
        return cls(
            codespaces=[CodeSpace(0x0000, 0xFFFF, 2)],
            cid_ranges=[CIDRange(0x0000, 0xFFFF, 2, 0)],
            vertical=vertical,
        )

    @classmethod
    def parse(cls, data: bytes) -> "CIDCMap":
        tokens = _tokens(data)
        codespaces: list[CodeSpace] = []
        cid_chars: dict[bytes, int] = {}
        ranges: list[CIDRange] = []
        vertical = False
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == b"/WMode" and index + 2 < len(tokens):
                try:
                    vertical = int(tokens[index + 1]) == 1
                except ValueError:
                    pass
                index += 1
            if token.isdigit() and index + 1 < len(tokens):
                count = int(token)
                operator = tokens[index + 1]
                if operator == b"begincodespacerange":
                    index += 2
                    for _ in range(count):
                        if index + 1 >= len(tokens):
                            raise CMapError("truncated codespacerange")
                        low_raw = _hex(tokens[index])
                        high_raw = _hex(tokens[index + 1])
                        if len(low_raw) != len(high_raw):
                            raise CMapError("codespace bounds have different lengths")
                        codespaces.append(
                            CodeSpace(
                                int.from_bytes(low_raw, "big"),
                                int.from_bytes(high_raw, "big"),
                                len(low_raw),
                            )
                        )
                        index += 2
                    continue
                if operator == b"begincidchar":
                    index += 2
                    for _ in range(count):
                        if index + 1 >= len(tokens):
                            raise CMapError("truncated cidchar")
                        raw = _hex(tokens[index])
                        try:
                            cid = int(tokens[index + 1])
                        except ValueError as exc:
                            raise CMapError("cidchar destination is not an integer") from exc
                        cid_chars[raw] = cid
                        index += 2
                    continue
                if operator == b"begincidrange":
                    index += 2
                    for _ in range(count):
                        if index + 2 >= len(tokens):
                            raise CMapError("truncated cidrange")
                        low_raw = _hex(tokens[index])
                        high_raw = _hex(tokens[index + 1])
                        try:
                            cid = int(tokens[index + 2])
                        except ValueError as exc:
                            raise CMapError("cidrange destination is not an integer") from exc
                        if len(low_raw) != len(high_raw):
                            raise CMapError("cidrange bounds have different lengths")
                        start = int.from_bytes(low_raw, "big")
                        end = int.from_bytes(high_raw, "big")
                        if end < start:
                            raise CMapError("cidrange end precedes start")
                        ranges.append(CIDRange(start, end, len(low_raw), cid))
                        index += 3
                    continue
            index += 1
        return cls(
            codespaces=codespaces,
            cid_chars=cid_chars,
            cid_ranges=ranges,
            vertical=vertical,
        )

    def code_to_cid(self, raw: bytes) -> int | None:
        if raw in self.cid_chars:
            return self.cid_chars[raw]
        for item in self.cid_ranges:
            cid = item.lookup(raw)
            if cid is not None:
                return cid
        return None

    def decode(self, data: bytes) -> list[tuple[bytes, int]]:
        output: list[tuple[bytes, int]] = []
        position = 0
        while position < len(data):
            matched = False
            for length in self._lengths:
                if position + length > len(data):
                    continue
                raw = data[position : position + length]
                if not any(space.accepts(raw) for space in self.codespaces):
                    continue
                cid = self.code_to_cid(raw)
                if cid is None:
                    raise CMapError(f"CMap contains no CID mapping for code <{raw.hex()}>")
                output.append((raw, cid))
                position += length
                matched = True
                break
            if not matched:
                raise CMapError(f"byte sequence at offset {position} is outside CMap codespaces")
        return output
