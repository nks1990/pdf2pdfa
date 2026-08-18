"""Pure-Python PDF file parser.

The parser supports classic cross-reference tables, cross-reference streams,
object streams, incremental-update /Prev chains and a conservative xref rebuild
fallback.  It deliberately keeps stream bytes encoded so codecs can be applied
only when a higher layer actually needs decoded data.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
import zlib
from typing import BinaryIO, Iterable, Iterator

from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream, as_int
from .tokenizer import PDFSyntaxError, PDFTokenizer


_HEADER_RE = re.compile(br"%PDF-(\d\.\d)")
_OBJECT_RE = re.compile(br"(?m)(?<![0-9])(\d+)\s+(\d+)\s+obj(?:\s|$)")
_STARTXREF_RE = re.compile(br"startxref\s+(\d+)\s+%%EOF", re.S)


class PDFParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class XRefEntry:
    kind: int  # 0 free, 1 uncompressed, 2 object-stream member
    field2: int
    field3: int

    @property
    def offset(self) -> int:
        return self.field2 if self.kind == 1 else -1

    @property
    def generation(self) -> int:
        return self.field3 if self.kind == 1 else 0

    @property
    def object_stream_number(self) -> int:
        return self.field2 if self.kind == 2 else -1

    @property
    def object_stream_index(self) -> int:
        return self.field3 if self.kind == 2 else -1


def _line_end(data: bytes, position: int) -> tuple[int, int]:
    end = data.find(b"\n", position)
    if end < 0:
        return len(data), len(data)
    content_end = end - 1 if end > position and data[end - 1] == 13 else end
    return content_end, end + 1


def _ascii_hex_decode(data: bytes) -> bytes:
    cleaned = bytearray()
    for byte in data:
        if byte in b"\x00\x09\x0a\x0c\x0d\x20":
            continue
        if byte == ord(">"):
            break
        cleaned.append(byte)
    if len(cleaned) % 2:
        cleaned.append(ord("0"))
    try:
        return bytes.fromhex(cleaned.decode("ascii"))
    except ValueError as exc:
        raise PDFParseError("Invalid ASCIIHexDecode stream") from exc


def _ascii85_decode(data: bytes) -> bytes:
    stripped = b"".join(data.split())
    if stripped.startswith(b"<~"):
        stripped = stripped[2:]
    if stripped.endswith(b"~>"):
        stripped = stripped[:-2]
    try:
        return base64.a85decode(stripped, adobe=False, ignorechars=b" \t\n\r\v")
    except ValueError as exc:
        raise PDFParseError("Invalid ASCII85Decode stream") from exc


def _run_length_decode(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        length = data[index]
        index += 1
        if length == 128:
            break
        if length <= 127:
            count = length + 1
            if index + count > len(data):
                raise PDFParseError("Truncated RunLengthDecode literal run")
            out.extend(data[index : index + count])
            index += count
        else:
            count = 257 - length
            if index >= len(data):
                raise PDFParseError("Truncated RunLengthDecode repeat run")
            out.extend([data[index]] * count)
            index += 1
    return bytes(out)


class _BitReader:
    __slots__ = ("data", "bit")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit = 0

    def read(self, width: int) -> int | None:
        if self.bit + width > len(self.data) * 8:
            return None
        value = 0
        for _ in range(width):
            byte_index = self.bit >> 3
            shift = 7 - (self.bit & 7)
            value = (value << 1) | ((self.data[byte_index] >> shift) & 1)
            self.bit += 1
        return value


def _lzw_decode(data: bytes, early_change: int = 1) -> bytes:
    clear = 256
    eod = 257
    reader = _BitReader(data)
    table: dict[int, bytes] = {index: bytes([index]) for index in range(256)}
    next_code = 258
    width = 9
    previous: bytes | None = None
    out = bytearray()

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
            raise PDFParseError(f"Invalid LZW code {code}")
        out.extend(entry)
        if previous is not None and next_code < 4096:
            table[next_code] = previous + entry[:1]
            next_code += 1
            if width < 12 and next_code + early_change == (1 << width):
                width += 1
        previous = entry
    return bytes(out)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _apply_predictor(data: bytes, parms: PDFDict | None) -> bytes:
    if not parms:
        return data
    predictor = as_int(parms.get("Predictor"), 1)
    if predictor <= 1:
        return data
    colors = max(1, as_int(parms.get("Colors"), 1))
    columns = max(1, as_int(parms.get("Columns"), 1))
    bits = max(1, as_int(parms.get("BitsPerComponent"), 8))
    row_bytes = (colors * columns * bits + 7) // 8
    bpp = max(1, (colors * bits + 7) // 8)

    if predictor == 2:
        if bits != 8:
            raise PDFParseError("TIFF predictor currently requires 8-bit components")
        if row_bytes == 0 or len(data) % row_bytes:
            raise PDFParseError("Invalid TIFF predictor row length")
        out = bytearray(data)
        for row in range(0, len(out), row_bytes):
            for index in range(bpp, row_bytes):
                out[row + index] = (out[row + index] + out[row + index - bpp]) & 0xFF
        return bytes(out)

    if not 10 <= predictor <= 15:
        raise PDFParseError(f"Unsupported predictor {predictor}")

    previous = bytearray(row_bytes)
    out = bytearray()
    position = 0
    while position < len(data):
        if predictor == 15:
            if position >= len(data):
                raise PDFParseError("Truncated PNG predictor row")
            filter_type = data[position]
            position += 1
        else:
            filter_type = predictor - 10
        if position + row_bytes > len(data):
            raise PDFParseError("Truncated PNG predictor payload")
        encoded = data[position : position + row_bytes]
        position += row_bytes
        row = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = row[index - bpp] if index >= bpp else 0
            up = previous[index]
            upper_left = previous[index - bpp] if index >= bpp else 0
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
                raise PDFParseError(f"Invalid PNG predictor filter {filter_type}")
            row[index] = decoded & 0xFF
        out.extend(row)
        previous = row
    return bytes(out)


def _filter_names(value: PDFObject | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, PDFName):
        return [value.value]
    if isinstance(value, list):
        return [item.value for item in value if isinstance(item, PDFName)]
    raise PDFParseError("Stream Filter shall be a name or array of names")


def _decode_parms(value: PDFObject | None, count: int) -> list[PDFDict | None]:
    if value is None:
        return [None] * count
    if isinstance(value, PDFDict):
        return [value] + [None] * max(0, count - 1)
    if isinstance(value, list):
        result: list[PDFDict | None] = []
        for item in value:
            result.append(item if isinstance(item, PDFDict) else None)
        result.extend([None] * max(0, count - len(result)))
        return result[:count]
    return [None] * count


def decode_general_stream(stream: PDFStream) -> bytes:
    """Decode lossless/general filters needed to parse PDF structure.

    Terminal image codecs such as DCT, JPX, JBIG2 and CCITT are deliberately
    not decoded by this structural layer.
    """
    data = stream.data
    filters = _filter_names(stream.get("Filter"))
    parms = _decode_parms(stream.get("DecodeParms"), len(filters))
    for name, params in zip(filters, parms):
        if name in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise PDFParseError("Invalid FlateDecode stream") from exc
            data = _apply_predictor(data, params)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = _ascii_hex_decode(data)
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85_decode(data)
        elif name in ("RunLengthDecode", "RL"):
            data = _run_length_decode(data)
        elif name in ("LZWDecode", "LZW"):
            early = as_int(params.get("EarlyChange"), 1) if params else 1
            data = _lzw_decode(data, early)
            data = _apply_predictor(data, params)
        else:
            raise PDFParseError(f"Structural stream uses unsupported filter /{name}")
    return data


class PDFDocument:
    """Random-access representation of a PDF file backed by owned parser code."""

    def __init__(self, data: bytes, *, repair: bool = True) -> None:
        self.data = data
        self.repair = repair
        self.header_version = self._read_header_version()
        self.xref: dict[int, XRefEntry] = {}
        self.trailer = PDFDict()
        self._cache: dict[PDFRef, PDFObject] = {}
        self._objstm_cache: dict[int, dict[int, PDFObject]] = {}
        self._overrides: dict[PDFRef, PDFObject | None] = {}
        self._load_cross_references()

    @classmethod
    def open(cls, source: str | Path | bytes | bytearray | BinaryIO, *, repair: bool = True) -> "PDFDocument":
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        elif isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            data = source.read()
        if not isinstance(data, bytes):
            data = bytes(data)
        return cls(data, repair=repair)

    def _read_header_version(self) -> str:
        match = _HEADER_RE.search(self.data[:1024])
        if not match:
            raise PDFParseError("Missing PDF header")
        return match.group(1).decode("ascii")

    def _find_startxref(self) -> int:
        tail = self.data[-65536:]
        matches = list(_STARTXREF_RE.finditer(tail))
        if matches:
            return int(matches[-1].group(1))
        marker = self.data.rfind(b"startxref")
        if marker < 0:
            raise PDFParseError("Missing startxref")
        tokenizer = PDFTokenizer(self.data, marker + len(b"startxref"))
        token = tokenizer.read_regular_token()
        try:
            return int(token)
        except ValueError as exc:
            raise PDFParseError("Invalid startxref offset") from exc

    def _load_cross_references(self) -> None:
        try:
            start = self._find_startxref()
            self._load_xref_section(start, seen=set(), newest=True)
            if "Root" not in self.trailer:
                raise PDFParseError("Trailer does not contain /Root")
        except Exception:
            if not self.repair:
                raise
            self._rebuild_xref()

    def _load_xref_section(self, offset: int, *, seen: set[int], newest: bool) -> None:
        if offset in seen:
            raise PDFParseError("Cross-reference /Prev chain contains a cycle")
        if offset < 0 or offset >= len(self.data):
            raise PDFParseError(f"Cross-reference offset {offset} is outside the file")
        seen.add(offset)
        position = offset
        while position < len(self.data) and self.data[position] in b"\x00\x09\x0a\x0c\x0d\x20":
            position += 1
        if self.data.startswith(b"xref", position):
            trailer, entries = self._parse_classic_xref(position)
        else:
            ref, obj, _ = self._parse_indirect_at(position, allow_length_scan=True)
            if not isinstance(obj, PDFStream) or not isinstance(obj.dictionary, PDFDict):
                raise PDFParseError("startxref does not point to xref table or xref stream")
            if not isinstance(obj.get("Type"), PDFName) or obj.get("Type").value != "XRef":  # type: ignore[union-attr]
                raise PDFParseError("startxref stream is not /Type /XRef")
            trailer = obj.dictionary
            entries = self._parse_xref_stream(obj)
            self._cache[ref] = obj

        for object_number, entry in entries.items():
            self.xref.setdefault(object_number, entry)
        for key, value in trailer.items():
            if key not in self.trailer:
                self.trailer[key] = value

        # Hybrid-reference files may place compressed-object entries in XRefStm.
        xref_stm = trailer.get("XRefStm")
        if isinstance(xref_stm, int) and xref_stm not in seen:
            self._load_xref_section(xref_stm, seen=seen, newest=False)

        prev = trailer.get("Prev")
        if isinstance(prev, int):
            self._load_xref_section(prev, seen=seen, newest=False)

    def _parse_classic_xref(self, offset: int) -> tuple[PDFDict, dict[int, XRefEntry]]:
        tokenizer = PDFTokenizer(self.data, offset)
        tokenizer.expect(b"xref")
        entries: dict[int, XRefEntry] = {}
        while True:
            tokenizer.skip_space()
            if tokenizer.peek_keyword(b"trailer"):
                tokenizer.expect(b"trailer")
                trailer = tokenizer.parse_object()
                if not isinstance(trailer, PDFDict):
                    raise PDFParseError("xref trailer is not a dictionary")
                return trailer, entries
            try:
                start = int(tokenizer.read_regular_token())
                count = int(tokenizer.read_regular_token())
            except (ValueError, PDFSyntaxError) as exc:
                raise PDFParseError("Invalid xref subsection header") from exc
            if start < 0 or count < 0:
                raise PDFParseError("Negative xref subsection")
            for index in range(count):
                tokenizer.skip_space()
                line_start = tokenizer.position
                content_end, next_line = _line_end(self.data, line_start)
                line = self.data[line_start:content_end].strip()
                tokenizer.position = next_line
                match = re.match(br"^(\d+)\s+(\d+)\s+([nf])", line)
                if not match:
                    raise PDFParseError(f"Invalid xref entry: {line!r}")
                field2 = int(match.group(1))
                field3 = int(match.group(2))
                kind = 1 if match.group(3) == b"n" else 0
                entries[start + index] = XRefEntry(kind, field2, field3)

    def _parse_xref_stream(self, stream: PDFStream) -> dict[int, XRefEntry]:
        widths_obj = stream.get("W")
        if not isinstance(widths_obj, list) or len(widths_obj) != 3:
            raise PDFParseError("XRef stream /W must contain three integers")
        widths = [as_int(value, -1) for value in widths_obj]
        if any(value < 0 for value in widths) or sum(widths) <= 0:
            raise PDFParseError("Invalid XRef stream /W")
        size = as_int(stream.get("Size"), -1)
        if size < 0:
            raise PDFParseError("XRef stream is missing /Size")
        index_obj = stream.get("Index")
        if index_obj is None:
            ranges = [0, size]
        elif isinstance(index_obj, list) and len(index_obj) % 2 == 0:
            ranges = [as_int(value, -1) for value in index_obj]
        else:
            raise PDFParseError("Invalid XRef stream /Index")
        if any(value < 0 for value in ranges):
            raise PDFParseError("Invalid XRef stream range")
        decoded = decode_general_stream(stream)
        row_width = sum(widths)
        entries: dict[int, XRefEntry] = {}
        position = 0
        for range_index in range(0, len(ranges), 2):
            first, count = ranges[range_index], ranges[range_index + 1]
            for object_number in range(first, first + count):
                if position + row_width > len(decoded):
                    raise PDFParseError("Truncated XRef stream")
                fields: list[int] = []
                for width_index, width in enumerate(widths):
                    if width == 0:
                        fields.append(1 if width_index == 0 else 0)
                        continue
                    value = int.from_bytes(decoded[position : position + width], "big")
                    position += width
                    fields.append(value)
                entries[object_number] = XRefEntry(fields[0], fields[1], fields[2])
        return entries

    def _rebuild_xref(self) -> None:
        self.xref.clear()
        for match in _OBJECT_RE.finditer(self.data):
            object_number = int(match.group(1))
            generation = int(match.group(2))
            self.xref[object_number] = XRefEntry(1, match.start(), generation)
        if not self.xref:
            raise PDFParseError("Could not rebuild cross-reference table")

        trailer_pos = self.data.rfind(b"trailer")
        if trailer_pos >= 0:
            try:
                tokenizer = PDFTokenizer(self.data, trailer_pos + len(b"trailer"))
                trailer = tokenizer.parse_object()
                if isinstance(trailer, PDFDict):
                    self.trailer = trailer
            except Exception:
                pass
        if "Root" not in self.trailer:
            # Recover a catalog by examining indirect dictionaries.
            for number in sorted(self.xref):
                try:
                    obj = self.get(PDFRef(number, self.xref[number].generation))
                except Exception:
                    continue
                if isinstance(obj, PDFDict):
                    obj_type = obj.get("Type")
                    if isinstance(obj_type, PDFName) and obj_type.value == "Catalog":
                        self.trailer["Root"] = PDFRef(number, self.xref[number].generation)
                        break
        if "Root" not in self.trailer:
            raise PDFParseError("Could not recover document catalog")
        self.trailer["Size"] = max(self.xref) + 1

    def _parse_indirect_at(
        self,
        offset: int,
        *,
        allow_length_scan: bool = False,
    ) -> tuple[PDFRef, PDFObject, int]:
        tokenizer = PDFTokenizer(self.data, offset)
        try:
            object_number = int(tokenizer.read_regular_token())
            generation = int(tokenizer.read_regular_token())
            tokenizer.expect(b"obj")
            value = tokenizer.parse_object()
        except (ValueError, PDFSyntaxError) as exc:
            raise PDFParseError(f"Invalid indirect object at offset {offset}") from exc
        ref = PDFRef(object_number, generation)
        tokenizer.skip_space()
        if isinstance(value, PDFDict) and tokenizer.peek_keyword(b"stream"):
            tokenizer.expect(b"stream")
            # The stream keyword is followed by EOL. Be lenient about spaces.
            while tokenizer.position < len(self.data) and self.data[tokenizer.position] in b" \t\x0c":
                tokenizer.position += 1
            if self.data.startswith(b"\r\n", tokenizer.position):
                tokenizer.position += 2
            elif tokenizer.position < len(self.data) and self.data[tokenizer.position] in (10, 13):
                tokenizer.position += 1
            data_start = tokenizer.position
            length_obj = value.get("Length")
            length: int | None = None
            if isinstance(length_obj, int):
                length = length_obj
            elif isinstance(length_obj, PDFRef) and not allow_length_scan:
                try:
                    resolved = self.get(length_obj)
                    if isinstance(resolved, int):
                        length = resolved
                except Exception:
                    length = None
            if length is not None and 0 <= length <= len(self.data) - data_start:
                stream_end = data_start + length
                probe = stream_end
                while probe < len(self.data) and self.data[probe] in b"\x00\x09\x0a\x0c\x0d\x20":
                    probe += 1
                if not self.data.startswith(b"endstream", probe):
                    if not allow_length_scan:
                        raise PDFParseError(f"Stream /Length is inconsistent for object {ref}")
                    length = None
            if length is None:
                marker = self.data.find(b"endstream", data_start)
                if marker < 0:
                    raise PDFParseError(f"Unterminated stream object {ref}")
                stream_end = marker
                while stream_end > data_start and self.data[stream_end - 1] in (10, 13):
                    stream_end -= 1
            value = PDFStream(value, self.data[data_start:stream_end])
            tokenizer.position = stream_end
            marker = self.data.find(b"endstream", tokenizer.position, min(len(self.data), tokenizer.position + 64))
            if marker < 0:
                marker = self.data.find(b"endstream", tokenizer.position)
            if marker < 0:
                raise PDFParseError(f"Missing endstream for object {ref}")
            tokenizer.position = marker + len(b"endstream")
        tokenizer.skip_space()
        if tokenizer.peek_keyword(b"endobj"):
            tokenizer.expect(b"endobj")
        elif not allow_length_scan:
            raise PDFParseError(f"Missing endobj for object {ref}")
        return ref, value, tokenizer.position

    def get(self, ref: PDFRef) -> PDFObject:
        if ref in self._overrides:
            value = self._overrides[ref]
            if value is None:
                raise KeyError(ref)
            return value
        if ref in self._cache:
            return self._cache[ref]
        entry = self.xref.get(ref.object_number)
        if entry is None or entry.kind == 0:
            raise KeyError(ref)
        if entry.kind == 1:
            parsed_ref, value, _ = self._parse_indirect_at(entry.offset)
            if parsed_ref.object_number != ref.object_number:
                raise PDFParseError(
                    f"xref for {ref.object_number} points to object {parsed_ref.object_number}"
                )
            self._cache[parsed_ref] = value
            # A caller may use generation 0 for compressed or rebuilt refs; the
            # actual parsed generation is authoritative.
            self._cache[ref] = value
            return value
        if entry.kind == 2:
            objects = self._load_object_stream(entry.object_stream_number)
            if ref.object_number not in objects:
                raise PDFParseError(
                    f"Object stream {entry.object_stream_number} does not contain {ref.object_number}"
                )
            value = objects[ref.object_number]
            self._cache[ref] = value
            return value
        raise PDFParseError(f"Unsupported xref entry type {entry.kind}")

    def _load_object_stream(self, object_stream_number: int) -> dict[int, PDFObject]:
        if object_stream_number in self._objstm_cache:
            return self._objstm_cache[object_stream_number]
        entry = self.xref.get(object_stream_number)
        if entry is None or entry.kind != 1:
            raise PDFParseError(f"Missing object stream {object_stream_number}")
        ref = PDFRef(object_stream_number, entry.generation)
        stream = self.get(ref)
        if not isinstance(stream, PDFStream):
            raise PDFParseError(f"Object {object_stream_number} is not a stream")
        obj_type = stream.get("Type")
        if not isinstance(obj_type, PDFName) or obj_type.value != "ObjStm":
            raise PDFParseError(f"Object {object_stream_number} is not /ObjStm")
        count = as_int(stream.get("N"), -1)
        first = as_int(stream.get("First"), -1)
        if count < 0 or first < 0:
            raise PDFParseError("ObjStm is missing /N or /First")
        decoded = decode_general_stream(stream)
        header = PDFTokenizer(decoded, 0, min(first, len(decoded)))
        pairs: list[tuple[int, int]] = []
        try:
            for _ in range(count):
                number = int(header.read_regular_token())
                relative = int(header.read_regular_token())
                pairs.append((number, relative))
        except (ValueError, PDFSyntaxError) as exc:
            raise PDFParseError("Malformed ObjStm header") from exc
        objects: dict[int, PDFObject] = {}
        for index, (number, relative) in enumerate(pairs):
            start = first + relative
            if start < first or start >= len(decoded):
                raise PDFParseError("ObjStm object offset is outside stream")
            end = len(decoded)
            if index + 1 < len(pairs):
                end = min(end, first + pairs[index + 1][1])
            tokenizer = PDFTokenizer(decoded, start, end)
            objects[number] = tokenizer.parse_object()
        self._objstm_cache[object_stream_number] = objects
        return objects

    def resolve(self, value: PDFObject) -> PDFObject:
        return self.get(value) if isinstance(value, PDFRef) else value

    @property
    def root_ref(self) -> PDFRef:
        root = self.trailer.get("Root")
        if not isinstance(root, PDFRef):
            raise PDFParseError("Trailer /Root is not an indirect reference")
        return root

    @property
    def catalog(self) -> PDFDict:
        root = self.get(self.root_ref)
        if not isinstance(root, PDFDict):
            raise PDFParseError("Document catalog is not a dictionary")
        return root

    def set(self, ref: PDFRef, value: PDFObject) -> None:
        self._overrides[ref] = value

    def delete(self, ref: PDFRef) -> None:
        self._overrides[ref] = None

    def new_object(self, value: PDFObject) -> PDFRef:
        used = set(self.xref)
        used.update(ref.object_number for ref in self._overrides)
        number = max(used, default=0) + 1
        ref = PDFRef(number, 0)
        self._overrides[ref] = value
        return ref

    def object_ref(self, object_number: int) -> PDFRef:
        entry = self.xref.get(object_number)
        if entry is None:
            return PDFRef(object_number, 0)
        return PDFRef(object_number, entry.generation)

    def iter_indirect_objects(self) -> Iterator[tuple[PDFRef, PDFObject]]:
        numbers = set(self.xref)
        numbers.update(ref.object_number for ref in self._overrides)
        for number in sorted(numbers):
            if number == 0:
                continue
            ref = self.object_ref(number)
            override_ref = next(
                (candidate for candidate in self._overrides if candidate.object_number == number),
                ref,
            )
            if override_ref in self._overrides:
                value = self._overrides[override_ref]
                if value is None:
                    continue
                yield override_ref, value
                continue
            entry = self.xref.get(number)
            if entry is None or entry.kind == 0:
                continue
            try:
                value = self.get(ref)
            except Exception:
                if self.repair:
                    continue
                raise
            # XRef/ObjStm container objects are serialization infrastructure;
            # their logical members are emitted individually by the native writer.
            if isinstance(value, PDFStream):
                obj_type = value.get("Type")
                if isinstance(obj_type, PDFName) and obj_type.value in ("XRef", "ObjStm"):
                    continue
            yield ref, value

    def reachable_refs(self) -> set[PDFRef]:
        """Return indirect objects reachable from trailer roots.

        This is used by the writer to drop stale incremental-update/private
        objects rather than blindly copying every historical object.
        """
        start: list[PDFObject] = []
        for key in ("Root", "Info"):
            value = self.trailer.get(key)
            if value is not None:
                start.append(value)
        seen_refs: set[PDFRef] = set()
        seen_direct: set[int] = set()

        def visit(value: PDFObject) -> None:
            if isinstance(value, PDFRef):
                if value in seen_refs:
                    return
                seen_refs.add(value)
                try:
                    visit(self.get(value))
                except Exception:
                    return
                return
            if isinstance(value, PDFStream):
                visit(value.dictionary)
                return
            if isinstance(value, PDFDict):
                identity = id(value)
                if identity in seen_direct:
                    return
                seen_direct.add(identity)
                for child in value.values():
                    visit(child)
                return
            if isinstance(value, list):
                identity = id(value)
                if identity in seen_direct:
                    return
                seen_direct.add(identity)
                for child in value:
                    visit(child)

        for value in start:
            visit(value)
        return seen_refs
