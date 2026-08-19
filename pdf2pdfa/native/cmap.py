"""Owned parser for CMaps that map Type0 character codes to CIDs.

The implementation supports local codespaces, cidchar/cidrange mappings,
notdefchar/notdefrange fallback mappings and a single inherited base CMap. A
base may come from a PDF stream `/UseCMap` dictionary entry or a PostScript
`/Name usecmap` operator. Child CID mappings take precedence over inherited CID
mappings; notdef mappings are consulted only when no ordinary CID mapping
exists anywhere in the inheritance chain.

Named CMaps are resolved only through an explicit owned registry callback;
unknown names never fall back to a system CMap.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Iterable


class CMapError(ValueError):
    pass


CMapRegistry = Callable[[str], "CIDCMap"]
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
    # CMap syntax is PostScript-like. For CID mapping we need names, integers,
    # arrays and hex strings. Procedures/literal strings are intentionally not
    # evaluated by this owned subset.
    data = re.sub(rb"%[^\r\n]*", b" ", data)
    pattern = re.compile(
        rb"<[^>]*>|\[|\]|/[A-Za-z0-9_.+-]+|-?\d+|[A-Za-z][A-Za-z0-9_.+-]*"
    )
    return pattern.findall(data)


@dataclass(frozen=True, slots=True)
class CodeSpace:
    low: int
    high: int
    length: int

    def accepts(self, raw: bytes) -> bool:
        return (
            len(raw) == self.length
            and self.low <= int.from_bytes(raw, "big") <= self.high
        )


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


@dataclass(frozen=True, slots=True)
class NotDefRange:
    start: int
    end: int
    code_length: int
    cid: int

    def lookup(self, raw: bytes) -> int | None:
        if len(raw) != self.code_length:
            return None
        code = int.from_bytes(raw, "big")
        if self.start <= code <= self.end:
            # Adobe CMap notdefrange maps every code in the range to ONE CID;
            # unlike cidrange the destination is not incremented.
            return self.cid
        return None


class CIDCMap:
    def __init__(
        self,
        *,
        codespaces: Iterable[CodeSpace] = (),
        cid_chars: dict[bytes, int] | None = None,
        cid_ranges: Iterable[CIDRange] = (),
        notdef_chars: dict[bytes, int] | None = None,
        notdef_ranges: Iterable[NotDefRange] = (),
        vertical: bool | None = None,
        base: "CIDCMap | None" = None,
        name: str = "",
    ) -> None:
        self.local_codespaces = tuple(codespaces)
        self.cid_chars = dict(cid_chars or {})
        self.cid_ranges = tuple(cid_ranges)
        self.notdef_chars = dict(notdef_chars or {})
        self.notdef_ranges = tuple(notdef_ranges)
        self.base = base
        self.name = name
        if not self.local_codespaces and base is None:
            raise CMapError("CMap has no codespace ranges and no base CMap")
        self.vertical = base.vertical if vertical is None and base is not None else bool(vertical)

        inherited = base.codespaces if base is not None else ()
        combined: list[CodeSpace] = []
        for item in (*self.local_codespaces, *inherited):
            if item not in combined:
                combined.append(item)
        self.codespaces = tuple(combined)
        self._lengths = tuple(
            sorted({space.length for space in self.codespaces}, reverse=True)
        )

    @classmethod
    def identity(cls, *, vertical: bool = False) -> "CIDCMap":
        return cls(
            codespaces=[CodeSpace(0x0000, 0xFFFF, 2)],
            cid_ranges=[CIDRange(0x0000, 0xFFFF, 2, 0)],
            vertical=vertical,
            name="Identity-V" if vertical else "Identity-H",
        )

    @classmethod
    def parse(
        cls,
        data: bytes,
        *,
        registry: CMapRegistry | None = None,
        base: "CIDCMap | None" = None,
        name: str = "",
    ) -> "CIDCMap":
        tokens = _tokens(data)
        codespaces: list[CodeSpace] = []
        cid_chars: dict[bytes, int] = {}
        ranges: list[CIDRange] = []
        notdef_chars: dict[bytes, int] = {}
        notdef_ranges: list[NotDefRange] = []
        vertical: bool | None = None
        content_base: CIDCMap | None = None
        index = 0

        while index < len(tokens):
            token = tokens[index]

            if token == b"/WMode":
                if index + 1 >= len(tokens):
                    raise CMapError("truncated /WMode")
                try:
                    mode = int(tokens[index + 1])
                except ValueError as exc:
                    raise CMapError("/WMode is not an integer") from exc
                if mode not in (0, 1):
                    raise CMapError("/WMode shall be 0 or 1")
                vertical = mode == 1
                index += 2
                continue

            if token == b"usecmap":
                if index == 0 or not tokens[index - 1].startswith(b"/"):
                    raise CMapError("usecmap requires a preceding CMap name")
                if content_base is not None:
                    raise CMapError("CMap contains more than one usecmap base")
                if base is not None:
                    raise CMapError(
                        "CMap defines both stream /UseCMap and content usecmap bases"
                    )
                if registry is None:
                    raise CMapError("CMap usecmap requires an owned predefined-CMap registry")
                base_name = tokens[index - 1][1:].decode("latin-1")
                try:
                    content_base = registry(base_name)
                except CMapError:
                    raise
                except Exception as exc:
                    raise CMapError(f"cannot resolve base CMap /{base_name}: {exc}") from exc
                index += 1
                continue

            if token.lstrip(b"-").isdigit() and index + 1 < len(tokens):
                count = int(token)
                if count < 0:
                    raise CMapError("CMap block count cannot be negative")
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
                        low = int.from_bytes(low_raw, "big")
                        high = int.from_bytes(high_raw, "big")
                        if high < low:
                            raise CMapError("codespace high bound precedes low bound")
                        codespaces.append(CodeSpace(low, high, len(low_raw)))
                        index += 2
                    continue
                if operator in {b"begincidchar", b"beginnotdefchar"}:
                    target = cid_chars if operator == b"begincidchar" else notdef_chars
                    label = "cidchar" if operator == b"begincidchar" else "notdefchar"
                    index += 2
                    for _ in range(count):
                        if index + 1 >= len(tokens):
                            raise CMapError(f"truncated {label}")
                        raw = _hex(tokens[index])
                        try:
                            cid = int(tokens[index + 1])
                        except ValueError as exc:
                            raise CMapError(f"{label} destination is not an integer") from exc
                        if cid < 0:
                            raise CMapError(f"{label} destination cannot be negative")
                        target[raw] = cid
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
                        if cid < 0:
                            raise CMapError("cidrange destination cannot be negative")
                        if len(low_raw) != len(high_raw):
                            raise CMapError("cidrange bounds have different lengths")
                        start = int.from_bytes(low_raw, "big")
                        end = int.from_bytes(high_raw, "big")
                        if end < start:
                            raise CMapError("cidrange end precedes start")
                        ranges.append(CIDRange(start, end, len(low_raw), cid))
                        index += 3
                    continue
                if operator == b"beginnotdefrange":
                    index += 2
                    for _ in range(count):
                        if index + 2 >= len(tokens):
                            raise CMapError("truncated notdefrange")
                        low_raw = _hex(tokens[index])
                        high_raw = _hex(tokens[index + 1])
                        try:
                            cid = int(tokens[index + 2])
                        except ValueError as exc:
                            raise CMapError("notdefrange destination is not an integer") from exc
                        if cid < 0:
                            raise CMapError("notdefrange destination cannot be negative")
                        if len(low_raw) != len(high_raw):
                            raise CMapError("notdefrange bounds have different lengths")
                        start = int.from_bytes(low_raw, "big")
                        end = int.from_bytes(high_raw, "big")
                        if end < start:
                            raise CMapError("notdefrange end precedes start")
                        notdef_ranges.append(NotDefRange(start, end, len(low_raw), cid))
                        index += 3
                    continue
            index += 1

        selected_base = content_base or base
        return cls(
            codespaces=codespaces,
            cid_chars=cid_chars,
            cid_ranges=ranges,
            notdef_chars=notdef_chars,
            notdef_ranges=notdef_ranges,
            vertical=vertical,
            base=selected_base,
            name=name,
        )

    def _defined_cid(self, raw: bytes) -> int | None:
        if raw in self.cid_chars:
            return self.cid_chars[raw]
        for item in self.cid_ranges:
            cid = item.lookup(raw)
            if cid is not None:
                return cid
        if self.base is not None:
            return self.base._defined_cid(raw)
        return None

    def _notdef_cid(self, raw: bytes) -> int | None:
        if raw in self.notdef_chars:
            return self.notdef_chars[raw]
        for item in self.notdef_ranges:
            cid = item.lookup(raw)
            if cid is not None:
                return cid
        if self.base is not None:
            return self.base._notdef_cid(raw)
        return None

    def code_to_cid(self, raw: bytes) -> int | None:
        # Ordinary mappings from child/base are authoritative. notdef is a
        # fallback only after the complete usecmap chain has no ordinary CID.
        cid = self._defined_cid(raw)
        return cid if cid is not None else self._notdef_cid(raw)

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
                    raise CMapError(
                        f"CMap contains no CID mapping for code <{raw.hex()}>"
                    )
                output.append((raw, cid))
                position += length
                matched = True
                break
            if not matched:
                raise CMapError(
                    f"byte sequence at offset {position} is outside CMap codespaces"
                )
        return output
