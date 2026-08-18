"""Pure-Python PDF page/form content-stream parser."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Iterator

from .objects import PDFDict, PDFName, PDFObject
from .tokenizer import PDFSyntaxError, PDFTokenizer
from .writer import serialize_object


class ContentStreamError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContentInstruction:
    operands: tuple[PDFObject, ...]
    operator: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class InlineImage:
    dictionary: PDFDict
    data: bytes
    start: int
    end: int

    @property
    def operator(self) -> str:
        return "INLINE IMAGE"


ContentItem = ContentInstruction | InlineImage


def _looks_like_object(tokenizer: PDFTokenizer) -> bool:
    tokenizer.skip_space()
    if tokenizer.position >= tokenizer.end:
        return False
    data = tokenizer.data
    position = tokenizer.position
    byte = data[position]
    if data.startswith(b"<<", position):
        return True
    if byte in b"[/<(":
        return True
    save = position
    try:
        token = tokenizer.read_regular_token()
    except PDFSyntaxError:
        tokenizer.position = save
        return False
    tokenizer.position = save
    if token in (b"true", b"false", b"null"):
        return True
    try:
        text = token.decode("ascii")
    except UnicodeDecodeError:
        return False
    if not text:
        return False
    if all(ch in "+-0123456789." for ch in text) and any(ch.isdigit() for ch in text):
        return True
    return False


def _consume_single_separator(tokenizer: PDFTokenizer) -> None:
    if tokenizer.position >= tokenizer.end:
        return
    if tokenizer.data.startswith(b"\r\n", tokenizer.position):
        tokenizer.position += 2
    elif tokenizer.data[tokenizer.position] in b"\x00\x09\x0a\x0c\x0d\x20":
        tokenizer.position += 1


def _find_inline_image_end(data: bytes, start: int, end: int) -> tuple[int, int]:
    """Locate the delimited EI token terminating an inline image.

    PDF inline image bytes are allowed to contain the byte sequence ``EI``.
    The grammar therefore requires token delimiters around the real terminator.
    We accept candidates preceded by whitespace and followed by whitespace or a
    PDF delimiter, matching the interoperability rule used by mainstream PDF
    processors while avoiding a blind ``find(b'EI')``.
    """
    position = start
    delimiters = b"\x00\x09\x0a\x0c\x0d\x20()<>[]{}/%"
    while position < end:
        marker = data.find(b"EI", position, end)
        if marker < 0:
            break
        before_ok = marker > start and data[marker - 1] in b"\x00\x09\x0a\x0c\x0d\x20"
        after = marker + 2
        after_ok = after >= end or data[after] in delimiters
        if before_ok and after_ok:
            payload_end = marker - 1
            # The one whitespace byte that delimits EI is syntax, not image data.
            if payload_end > start and data[payload_end - 1] == 13 and data[marker - 1] == 10:
                payload_end -= 1
            return payload_end, after
        position = marker + 2
    raise ContentStreamError("Unterminated inline image")


def _parse_inline_image(tokenizer: PDFTokenizer, start: int) -> InlineImage:
    dictionary = PDFDict()
    while True:
        tokenizer.skip_space()
        save = tokenizer.position
        token = tokenizer.read_regular_token()
        if token == b"ID":
            _consume_single_separator(tokenizer)
            data_start = tokenizer.position
            data_end, syntax_end = _find_inline_image_end(
                tokenizer.data, data_start, tokenizer.end
            )
            payload = tokenizer.data[data_start:data_end]
            tokenizer.position = syntax_end
            return InlineImage(dictionary=dictionary, data=payload, start=start, end=syntax_end)
        tokenizer.position = save
        key = tokenizer.parse_object()
        if not isinstance(key, PDFName):
            raise ContentStreamError("Inline-image dictionary key is not a name")
        value = tokenizer.parse_object()
        dictionary[key.value] = value


def parse_content_stream(data: bytes) -> list[ContentItem]:
    tokenizer = PDFTokenizer(data)
    output: list[ContentItem] = []
    operands: list[PDFObject] = []
    instruction_start: int | None = None

    while not tokenizer.eof():
        tokenizer.skip_space()
        if instruction_start is None:
            instruction_start = tokenizer.position
        if _looks_like_object(tokenizer):
            try:
                operands.append(tokenizer.parse_object())
            except PDFSyntaxError as exc:
                raise ContentStreamError(str(exc)) from exc
            continue

        operator_start = tokenizer.position
        try:
            raw_operator = tokenizer.read_regular_token()
        except PDFSyntaxError as exc:
            raise ContentStreamError(str(exc)) from exc
        if not raw_operator:
            raise ContentStreamError(f"Empty content operator at {operator_start}")
        try:
            operator = raw_operator.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ContentStreamError(
                f"Content operator at {operator_start} is not ASCII"
            ) from exc

        if operator == "BI":
            if operands:
                raise ContentStreamError("BI inline-image operator cannot consume operands")
            output.append(_parse_inline_image(tokenizer, instruction_start))
            instruction_start = None
            continue

        output.append(
            ContentInstruction(
                operands=tuple(operands),
                operator=operator,
                start=instruction_start,
                end=tokenizer.position,
            )
        )
        operands.clear()
        instruction_start = None

    if operands:
        raise ContentStreamError("Content stream ends with operands but no operator")
    return output


def serialize_content_stream(items: Iterable[ContentItem]) -> bytes:
    output = bytearray()
    for item in items:
        if isinstance(item, InlineImage):
            output.extend(b"BI\n")
            for key in sorted(item.dictionary):
                output.extend(serialize_object(PDFName(key)))
                output.extend(b" ")
                output.extend(serialize_object(item.dictionary[key]))
                output.extend(b"\n")
            output.extend(b"ID\n")
            output.extend(item.data)
            output.extend(b"\nEI\n")
            continue
        if item.operands:
            output.extend(b" ".join(serialize_object(value) for value in item.operands))
            output.extend(b" ")
        output.extend(item.operator.encode("ascii"))
        output.extend(b"\n")
    return bytes(output)


def operators(items: Iterable[ContentItem], *names: str) -> Iterator[ContentInstruction]:
    wanted = set(names)
    for item in items:
        if isinstance(item, ContentInstruction) and (not wanted or item.operator in wanted):
            yield item
