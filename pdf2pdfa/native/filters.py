"""Pure-Python PDF stream filters.

These codecs cover the generalized/lossless filters needed to inspect and
normalize arbitrary PDF object/content streams without qpdf, pikepdf or an
external executable. Terminal image codecs remain explicitly classified so the
renderer can own them separately rather than accidentally treating compressed
image bytes as decoded samples.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import zlib
from typing import Mapping, Sequence


class StreamFilterError(ValueError):
    pass


class UnsupportedStreamFilterError(StreamFilterError):
    pass


TERMINAL_IMAGE_FILTERS = {
    "DCTDecode",
    "DCT",
    "JPXDecode",
    "JBIG2Decode",
    "CCITTFaxDecode",
    "CCF",
}

LOSSLESS_GENERAL_FILTERS = {
    "FlateDecode",
    "Fl",
    "LZWDecode",
    "LZW",
    "ASCIIHexDecode",
    "AHx",
    "ASCII85Decode",
    "A85",
    "RunLengthDecode",
    "RL",
}


@dataclass(frozen=True, slots=True)
class PredictorParams:
    predictor: int = 1
    colors: int = 1
    bits_per_component: int = 8
    columns: int = 1
    early_change: int = 1

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object] | None) -> "PredictorParams":
        if not mapping:
            return cls()

        def integer(key: str, default: int) -> int:
            value = mapping.get(key, mapping.get("/" + key, default))
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        return cls(
            predictor=integer("Predictor", 1),
            colors=max(1, integer("Colors", 1)),
            bits_per_component=max(1, integer("BitsPerComponent", 8)),
            columns=max(1, integer("Columns", 1)),
            early_change=integer("EarlyChange", 1),
        )


def ascii_hex_decode(data: bytes) -> bytes:
    digits = bytearray()
    for byte in data:
        if byte in b"\x00\x09\x0a\x0c\x0d\x20":
            continue
        if byte == ord(">"):
            break
        if not (48 <= byte <= 57 or 65 <= byte <= 70 or 97 <= byte <= 102):
            raise StreamFilterError(f"invalid ASCIIHex digit 0x{byte:02x}")
        digits.append(byte)
    if len(digits) & 1:
        digits.append(ord("0"))
    return bytes.fromhex(digits.decode("ascii"))


def ascii_hex_encode(data: bytes) -> bytes:
    return data.hex().upper().encode("ascii") + b">"


def ascii85_decode(data: bytes) -> bytes:
    compact = b"".join(data.split())
    if compact.startswith(b"<~"):
        compact = compact[2:]
    if compact.endswith(b"~>"):
        compact = compact[:-2]
    try:
        return base64.a85decode(compact, adobe=False, ignorechars=b" \t\n\r\v")
    except (ValueError, OverflowError) as exc:
        raise StreamFilterError("invalid ASCII85 stream") from exc


def ascii85_encode(data: bytes) -> bytes:
    return base64.a85encode(data, adobe=False) + b"~>"


def run_length_decode(data: bytes) -> bytes:
    output = bytearray()
    position = 0
    while position < len(data):
        control = data[position]
        position += 1
        if control == 128:
            break
        if control <= 127:
            count = control + 1
            if position + count > len(data):
                raise StreamFilterError("truncated RunLength literal run")
            output.extend(data[position : position + count])
            position += count
        else:
            count = 257 - control
            if position >= len(data):
                raise StreamFilterError("truncated RunLength repeat run")
            output.extend([data[position]] * count)
            position += 1
    return bytes(output)


def run_length_encode(data: bytes) -> bytes:
    output = bytearray()
    position = 0
    while position < len(data):
        # Prefer repeated runs of length >= 3.
        repeat = 1
        while (
            position + repeat < len(data)
            and data[position + repeat] == data[position]
            and repeat < 128
        ):
            repeat += 1
        if repeat >= 3:
            output.append(257 - repeat)
            output.append(data[position])
            position += repeat
            continue

        literal_start = position
        position += repeat
        while position < len(data) and position - literal_start < 128:
            lookahead = 1
            while (
                position + lookahead < len(data)
                and data[position + lookahead] == data[position]
                and lookahead < 128
            ):
                lookahead += 1
            if lookahead >= 3:
                break
            position += lookahead
        literal = data[literal_start:position]
        output.append(len(literal) - 1)
        output.extend(literal)
    output.append(128)
    return bytes(output)


class _BitReader:
    __slots__ = ("data", "bit_offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_offset = 0

    def read(self, width: int) -> int | None:
        if self.bit_offset + width > len(self.data) * 8:
            return None
        value = 0
        for _ in range(width):
            byte = self.data[self.bit_offset >> 3]
            shift = 7 - (self.bit_offset & 7)
            value = (value << 1) | ((byte >> shift) & 1)
            self.bit_offset += 1
        return value


def lzw_decode(data: bytes, *, early_change: int = 1) -> bytes:
    if early_change not in (0, 1):
        raise StreamFilterError("LZW EarlyChange must be 0 or 1")
    clear, eod = 256, 257
    table: dict[int, bytes] = {index: bytes([index]) for index in range(256)}
    next_code = 258
    width = 9
    previous: bytes | None = None
    reader = _BitReader(data)
    output = bytearray()

    while True:
        code = reader.read(width)
        if code is None:
            break
        if code == clear:
            table = {index: bytes([index]) for index in range(256)}
            next_code = 258
            width = 9
            previous = None
            continue
        if code == eod:
            break
        if code in table:
            entry = table[code]
        elif code == next_code and previous is not None:
            entry = previous + previous[:1]
        else:
            raise StreamFilterError(f"invalid LZW code {code}")
        output.extend(entry)
        if previous is not None and next_code < 4096:
            table[next_code] = previous + entry[:1]
            next_code += 1
            if width < 12 and next_code + early_change == (1 << width):
                width += 1
        previous = entry
    return bytes(output)


def _paeth(left: int, up: int, upper_left: int) -> int:
    p = left + up - upper_left
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return upper_left


def decode_predictor(data: bytes, params: PredictorParams) -> bytes:
    predictor = params.predictor
    if predictor <= 1:
        return data
    row_bytes = (params.colors * params.columns * params.bits_per_component + 7) // 8
    bytes_per_pixel = max(1, (params.colors * params.bits_per_component + 7) // 8)
    if row_bytes <= 0:
        raise StreamFilterError("predictor row size is zero")

    if predictor == 2:
        if params.bits_per_component != 8:
            raise UnsupportedStreamFilterError(
                "TIFF Predictor 2 with non-8-bit components is not implemented"
            )
        if len(data) % row_bytes:
            raise StreamFilterError("TIFF predictor data is not row-aligned")
        output = bytearray(data)
        for row_start in range(0, len(output), row_bytes):
            for index in range(bytes_per_pixel, row_bytes):
                target = row_start + index
                output[target] = (output[target] + output[target - bytes_per_pixel]) & 0xFF
        return bytes(output)

    if predictor < 10 or predictor > 15:
        raise UnsupportedStreamFilterError(f"unsupported Predictor {predictor}")

    output = bytearray()
    previous = bytearray(row_bytes)
    position = 0
    while position < len(data):
        if predictor == 15:
            filter_type = data[position]
            position += 1
        else:
            filter_type = predictor - 10
        if position + row_bytes > len(data):
            raise StreamFilterError("truncated PNG predictor row")
        encoded = data[position : position + row_bytes]
        position += row_bytes
        row = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + up
            elif filter_type == 3:
                decoded = value + ((left + up) // 2)
            elif filter_type == 4:
                decoded = value + _paeth(left, up, upper_left)
            else:
                raise StreamFilterError(f"invalid PNG predictor filter {filter_type}")
            row[index] = decoded & 0xFF
        output.extend(row)
        previous = row
    return bytes(output)


def encode_png_up_predictor(data: bytes, *, columns: int, colors: int = 1, bits_per_component: int = 8) -> bytes:
    """Encode fixed PNG Up predictor (PDF Predictor 12).

    This deterministic encoder is useful for compact xref/data tables produced
    by our own writer. The general converter normally emits ordinary Flate with
    Predictor 1 unless a format-specific encoder asks for prediction.
    """
    row_bytes = (colors * columns * bits_per_component + 7) // 8
    if row_bytes <= 0 or len(data) % row_bytes:
        raise StreamFilterError("predictor input is not row-aligned")
    previous = bytes(row_bytes)
    output = bytearray()
    for start in range(0, len(data), row_bytes):
        row = data[start : start + row_bytes]
        output.extend((value - previous[index]) & 0xFF for index, value in enumerate(row))
        previous = row
    return bytes(output)


def decode_pipeline(
    data: bytes,
    filters: Sequence[str],
    decode_parms: Sequence[Mapping[str, object] | None] | None = None,
) -> bytes:
    params_list = list(decode_parms or ())
    params_list.extend([None] * max(0, len(filters) - len(params_list)))
    for filter_name, mapping in zip(filters, params_list):
        name = filter_name.lstrip("/")
        params = PredictorParams.from_mapping(mapping)
        if name in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise StreamFilterError("invalid FlateDecode stream") from exc
            data = decode_predictor(data, params)
        elif name in ("LZWDecode", "LZW"):
            data = lzw_decode(data, early_change=params.early_change)
            data = decode_predictor(data, params)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = ascii_hex_decode(data)
        elif name in ("ASCII85Decode", "A85"):
            data = ascii85_decode(data)
        elif name in ("RunLengthDecode", "RL"):
            data = run_length_decode(data)
        elif name in TERMINAL_IMAGE_FILTERS:
            raise UnsupportedStreamFilterError(
                f"/{name} is a terminal image codec and must be decoded by the native image engine"
            )
        else:
            raise UnsupportedStreamFilterError(f"unsupported PDF stream filter /{name}")
    return data


def flate_encode(data: bytes, *, level: int = 9) -> bytes:
    if not 0 <= level <= 9:
        raise ValueError("zlib level must be between 0 and 9")
    return zlib.compress(data, level)


def normalize_general_stream(
    data: bytes,
    filters: Sequence[str],
    decode_parms: Sequence[Mapping[str, object] | None] | None = None,
) -> tuple[bytes, list[str], list[Mapping[str, object] | None]]:
    """Decode a generalized filter chain and emit deterministic Flate bytes."""
    decoded = decode_pipeline(data, filters, decode_parms)
    return flate_encode(decoded), ["FlateDecode"], [None]
