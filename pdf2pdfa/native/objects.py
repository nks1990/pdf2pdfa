"""Core PDF COS object types.

Names are stored as latin-1 strings because latin-1 is a bijection over byte
values 0..255.  This preserves arbitrary PDF name bytes without making the
higher layers operate on raw byte strings everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Iterator, MutableMapping, TypeAlias


@dataclass(frozen=True, slots=True, order=True)
class PDFName:
    value: str

    def __post_init__(self) -> None:
        value = self.value[1:] if self.value.startswith("/") else self.value
        object.__setattr__(self, "value", value)

    def __str__(self) -> str:
        return f"/{self.value}"


@dataclass(frozen=True, slots=True, order=True)
class PDFRef:
    object_number: int
    generation: int = 0

    def __post_init__(self) -> None:
        if self.object_number <= 0:
            raise ValueError("PDF object numbers are positive")
        if self.generation < 0:
            raise ValueError("PDF generation numbers are non-negative")

    def __str__(self) -> str:
        return f"{self.object_number} {self.generation} R"


class PDFDict(dict[str, "PDFObject"]):
    """PDF dictionary using unprefixed latin-1 name strings as keys."""

    @staticmethod
    def _key(key: str | PDFName) -> str:
        if isinstance(key, PDFName):
            return key.value
        return key[1:] if key.startswith("/") else key

    def __getitem__(self, key: str | PDFName) -> "PDFObject":
        return super().__getitem__(self._key(key))

    def __setitem__(self, key: str | PDFName, value: "PDFObject") -> None:
        super().__setitem__(self._key(key), value)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, (str, PDFName)):
            key = self._key(key)
        return super().__contains__(key)

    def get(self, key: str | PDFName, default: Any = None) -> Any:
        return super().get(self._key(key), default)

    def pop(self, key: str | PDFName, default: Any = None) -> Any:
        return super().pop(self._key(key), default)


@dataclass(slots=True)
class PDFStream:
    dictionary: PDFDict
    data: bytes

    def get(self, key: str | PDFName, default: Any = None) -> Any:
        return self.dictionary.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.dictionary

    def __getitem__(self, key: str | PDFName) -> "PDFObject":
        return self.dictionary[key]

    def __setitem__(self, key: str | PDFName, value: "PDFObject") -> None:
        self.dictionary[key] = value


PDFScalar: TypeAlias = None | bool | int | Decimal | bytes | PDFName | PDFRef
PDFObject: TypeAlias = PDFScalar | list["PDFObject"] | PDFDict | PDFStream


def as_name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def as_int(value: PDFObject | None, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return default


def clone_object(value: PDFObject) -> PDFObject:
    """Deep-copy a COS value while keeping indirect references as references."""
    if isinstance(value, PDFStream):
        return PDFStream(clone_object(value.dictionary), bytes(value.data))  # type: ignore[arg-type]
    if isinstance(value, PDFDict):
        return PDFDict({key: clone_object(item) for key, item in value.items()})
    if isinstance(value, list):
        return [clone_object(item) for item in value]
    return value


def walk_direct(value: PDFObject) -> Iterator[PDFObject]:
    """Walk only direct children; indirect references are yielded, not resolved."""
    yield value
    if isinstance(value, PDFStream):
        yield from walk_direct(value.dictionary)
    elif isinstance(value, PDFDict):
        for item in value.values():
            yield from walk_direct(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_direct(item)
