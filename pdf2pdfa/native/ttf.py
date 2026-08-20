"""Owned SFNT/TrueType/OpenType parser used for safe font embedding.

The parser intentionally focuses on information needed by PDF/A conversion:
font identity, embedding permissions, metrics, Unicode cmap and outline type.
It does not depend on fontTools or platform font APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Iterable


class FontParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TableRecord:
    tag: str
    checksum: int
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class FontMetrics:
    units_per_em: int
    ascent: int
    descent: int
    bbox: tuple[int, int, int, int]
    cap_height: int
    italic_angle: float
    weight_class: int
    fixed_pitch: bool

    def scale1000(self, value: int) -> int:
        return int(round(value * 1000 / self.units_per_em))

    @property
    def pdf_bbox(self) -> tuple[int, int, int, int]:
        return tuple(self.scale1000(value) for value in self.bbox)  # type: ignore[return-value]

    @property
    def pdf_ascent(self) -> int:
        return self.scale1000(self.ascent)

    @property
    def pdf_descent(self) -> int:
        return self.scale1000(self.descent)

    @property
    def pdf_cap_height(self) -> int:
        return self.scale1000(self.cap_height)


class SFNTFont:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sfnt_version: bytes
        self.tables: dict[str, TableRecord]
        self.sfnt_version, self.tables = self._directory()
        self.is_truetype = self.sfnt_version in (b"\x00\x01\x00\x00", b"true") and "glyf" in self.tables
        self.is_cff = self.sfnt_version == b"OTTO" or "CFF " in self.tables or "CFF2" in self.tables
        if not (self.is_truetype or self.is_cff):
            raise FontParseError("unsupported SFNT outline flavor")

    @classmethod
    def open(cls, source: str | Path | bytes) -> "SFNTFont":
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        else:
            data = bytes(source)
        return cls(data)

    def _directory(self) -> tuple[bytes, dict[str, TableRecord]]:
        if len(self.data) < 12:
            raise FontParseError("SFNT header is truncated")
        version = self.data[0:4]
        if version == b"ttcf":
            raise FontParseError("TrueType Collection requires an explicit face selector")
        num_tables = self._u16(4)
        if num_tables <= 0 or num_tables > 4096:
            raise FontParseError(f"invalid SFNT table count {num_tables}")
        end = 12 + 16 * num_tables
        if end > len(self.data):
            raise FontParseError("SFNT table directory is truncated")
        tables: dict[str, TableRecord] = {}
        for index in range(num_tables):
            base = 12 + 16 * index
            tag = self.data[base : base + 4].decode("latin-1")
            checksum = self._u32(base + 4)
            offset = self._u32(base + 8)
            length = self._u32(base + 12)
            if offset + length > len(self.data):
                raise FontParseError(f"SFNT table {tag!r} points outside font data")
            tables[tag] = TableRecord(tag, checksum, offset, length)
        for required in ("head", "maxp", "name", "hhea", "hmtx"):
            if required not in tables:
                raise FontParseError(f"SFNT font is missing required table {required!r}")
        return version, tables

    def _slice(self, tag: str) -> bytes:
        try:
            record = self.tables[tag]
        except KeyError as exc:
            raise FontParseError(f"font does not contain table {tag!r}") from exc
        return self.data[record.offset : record.offset + record.length]

    def _u16(self, offset: int) -> int:
        if offset + 2 > len(self.data):
            raise FontParseError("font read outside buffer")
        return int.from_bytes(self.data[offset : offset + 2], "big")

    def _i16(self, offset: int) -> int:
        if offset + 2 > len(self.data):
            raise FontParseError("font read outside buffer")
        return int.from_bytes(self.data[offset : offset + 2], "big", signed=True)

    def _u32(self, offset: int) -> int:
        if offset + 4 > len(self.data):
            raise FontParseError("font read outside buffer")
        return int.from_bytes(self.data[offset : offset + 4], "big")

    @staticmethod
    def _tu16(data: bytes, offset: int) -> int:
        if offset + 2 > len(data):
            raise FontParseError("font table read outside buffer")
        return int.from_bytes(data[offset : offset + 2], "big")

    @staticmethod
    def _ti16(data: bytes, offset: int) -> int:
        if offset + 2 > len(data):
            raise FontParseError("font table read outside buffer")
        return int.from_bytes(data[offset : offset + 2], "big", signed=True)

    @staticmethod
    def _tu32(data: bytes, offset: int) -> int:
        if offset + 4 > len(data):
            raise FontParseError("font table read outside buffer")
        return int.from_bytes(data[offset : offset + 4], "big")

    @property
    def units_per_em(self) -> int:
        head = self._slice("head")
        value = self._tu16(head, 18)
        if not 16 <= value <= 16384:
            raise FontParseError(f"invalid unitsPerEm {value}")
        return value

    @property
    def glyph_count(self) -> int:
        maxp = self._slice("maxp")
        if len(maxp) < 6:
            raise FontParseError("maxp table is truncated")
        return self._tu16(maxp, 4)

    @property
    def embedding_fstype(self) -> int:
        if "OS/2" not in self.tables:
            return 0
        os2 = self._slice("OS/2")
        if len(os2) < 10:
            raise FontParseError("OS/2 table is truncated")
        return self._tu16(os2, 8)

    @property
    def embeddable(self) -> bool:
        fs_type = self.embedding_fstype
        restricted = bool(fs_type & 0x0002)
        bitmap_only = bool(fs_type & 0x0200)
        return not restricted and not bitmap_only

    @property
    def no_subsetting(self) -> bool:
        return bool(self.embedding_fstype & 0x0100)

    def names(self, name_id: int) -> list[str]:
        table = self._slice("name")
        if len(table) < 6:
            raise FontParseError("name table is truncated")
        count = self._tu16(table, 2)
        string_offset = self._tu16(table, 4)
        records_end = 6 + count * 12
        if records_end > len(table) or string_offset > len(table):
            raise FontParseError("name table offsets are invalid")
        values: list[tuple[int, int, str]] = []
        for index in range(count):
            base = 6 + index * 12
            platform = self._tu16(table, base)
            encoding = self._tu16(table, base + 2)
            language = self._tu16(table, base + 4)
            candidate_id = self._tu16(table, base + 6)
            length = self._tu16(table, base + 8)
            offset = self._tu16(table, base + 10)
            if candidate_id != name_id:
                continue
            start = string_offset + offset
            end = start + length
            if start < string_offset or end > len(table):
                continue
            raw = table[start:end]
            try:
                if platform in (0, 3):
                    text = raw.decode("utf-16-be")
                    priority = 0 if platform == 3 and language in (0x0409, 0) else 1
                elif platform == 1:
                    text = raw.decode("mac_roman")
                    priority = 2
                else:
                    text = raw.decode("latin-1")
                    priority = 3
            except UnicodeDecodeError:
                continue
            text = text.replace("\x00", "").strip()
            if text:
                values.append((priority, language, text))
        result: list[str] = []
        for _priority, _language, text in sorted(values):
            if text not in result:
                result.append(text)
        return result

    @property
    def postscript_name(self) -> str | None:
        values = self.names(6)
        return values[0] if values else None

    @property
    def family_name(self) -> str | None:
        values = self.names(1)
        return values[0] if values else None

    @property
    def full_name(self) -> str | None:
        values = self.names(4)
        return values[0] if values else None

    @property
    def metrics(self) -> FontMetrics:
        head = self._slice("head")
        hhea = self._slice("hhea")
        if len(head) < 54 or len(hhea) < 36:
            raise FontParseError("head/hhea table is truncated")
        units = self.units_per_em
        bbox = (
            self._ti16(head, 36),
            self._ti16(head, 38),
            self._ti16(head, 40),
            self._ti16(head, 42),
        )
        ascent = self._ti16(hhea, 4)
        descent = self._ti16(hhea, 6)
        cap_height = ascent
        weight_class = 400
        if "OS/2" in self.tables:
            os2 = self._slice("OS/2")
            if len(os2) >= 6:
                weight_class = self._tu16(os2, 4)
            version = self._tu16(os2, 0) if len(os2) >= 2 else 0
            if version >= 2 and len(os2) >= 90:
                cap_height = self._ti16(os2, 88)
        italic_angle = 0.0
        fixed_pitch = False
        if "post" in self.tables:
            post = self._slice("post")
            if len(post) >= 16:
                raw_angle = int.from_bytes(post[4:8], "big", signed=True)
                italic_angle = raw_angle / 65536.0
                fixed_pitch = self._tu32(post, 12) != 0
        return FontMetrics(
            units_per_em=units,
            ascent=ascent,
            descent=descent,
            bbox=bbox,
            cap_height=cap_height,
            italic_angle=italic_angle,
            weight_class=weight_class,
            fixed_pitch=fixed_pitch,
        )

    def horizontal_metrics(self) -> list[tuple[int, int]]:
        hhea = self._slice("hhea")
        num_hmetrics = self._tu16(hhea, 34)
        glyph_count = self.glyph_count
        if num_hmetrics <= 0 or num_hmetrics > glyph_count:
            raise FontParseError("invalid numberOfHMetrics")
        hmtx = self._slice("hmtx")
        required = num_hmetrics * 4 + max(0, glyph_count - num_hmetrics) * 2
        if required > len(hmtx):
            raise FontParseError("hmtx table is truncated")
        result: list[tuple[int, int]] = []
        last_advance = 0
        for glyph in range(glyph_count):
            if glyph < num_hmetrics:
                base = glyph * 4
                last_advance = self._tu16(hmtx, base)
                lsb = self._ti16(hmtx, base + 2)
            else:
                base = num_hmetrics * 4 + (glyph - num_hmetrics) * 2
                lsb = self._ti16(hmtx, base)
            result.append((last_advance, lsb))
        return result

    def cmap(self) -> dict[int, int]:
        table = self._slice("cmap")
        if len(table) < 4:
            raise FontParseError("cmap table is truncated")
        count = self._tu16(table, 2)
        if 4 + 8 * count > len(table):
            raise FontParseError("cmap encoding records are truncated")
        candidates: list[tuple[int, int, int]] = []
        for index in range(count):
            base = 4 + index * 8
            platform = self._tu16(table, base)
            encoding = self._tu16(table, base + 2)
            offset = self._tu32(table, base + 4)
            if offset + 2 > len(table):
                continue
            fmt = self._tu16(table, offset)
            if fmt not in (4, 12):
                continue
            if platform == 3 and encoding == 10 and fmt == 12:
                priority = 0
            elif platform == 0 and fmt == 12:
                priority = 1
            elif platform == 3 and encoding in (1, 0) and fmt == 4:
                priority = 2
            elif platform == 0 and fmt == 4:
                priority = 3
            else:
                priority = 9
            candidates.append((priority, fmt, offset))
        if not candidates:
            raise FontParseError("font has no supported Unicode cmap (format 4/12)")
        _priority, fmt, offset = min(candidates)
        return self._cmap12(table, offset) if fmt == 12 else self._cmap4(table, offset)

    def _cmap12(self, table: bytes, offset: int) -> dict[int, int]:
        if offset + 16 > len(table):
            raise FontParseError("cmap format 12 is truncated")
        length = self._tu32(table, offset + 4)
        groups = self._tu32(table, offset + 12)
        if length < 16 or offset + length > len(table) or 16 + groups * 12 > length:
            raise FontParseError("cmap format 12 length/group count is invalid")
        result: dict[int, int] = {}
        for index in range(groups):
            base = offset + 16 + index * 12
            start = self._tu32(table, base)
            end = self._tu32(table, base + 4)
            start_gid = self._tu32(table, base + 8)
            if end < start or end > 0x10FFFF:
                raise FontParseError("cmap format 12 group is invalid")
            if end - start > 1_000_000:
                raise FontParseError("cmap format 12 group is unreasonably large")
            for codepoint in range(start, end + 1):
                result[codepoint] = start_gid + (codepoint - start)
        return result

    def _cmap4(self, table: bytes, offset: int) -> dict[int, int]:
        if offset + 14 > len(table):
            raise FontParseError("cmap format 4 is truncated")
        length = self._tu16(table, offset + 2)
        if length < 16 or offset + length > len(table):
            raise FontParseError("cmap format 4 length is invalid")
        segment_count = self._tu16(table, offset + 6) // 2
        if segment_count <= 0:
            raise FontParseError("cmap format 4 has no segments")
        end_codes = offset + 14
        reserved_pad = end_codes + 2 * segment_count
        start_codes = reserved_pad + 2
        id_deltas = start_codes + 2 * segment_count
        id_range_offsets = id_deltas + 2 * segment_count
        glyph_array = id_range_offsets + 2 * segment_count
        if glyph_array > offset + length:
            raise FontParseError("cmap format 4 arrays are truncated")
        result: dict[int, int] = {}
        for segment in range(segment_count):
            end_code = self._tu16(table, end_codes + 2 * segment)
            start_code = self._tu16(table, start_codes + 2 * segment)
            delta = self._ti16(table, id_deltas + 2 * segment)
            range_offset = self._tu16(table, id_range_offsets + 2 * segment)
            if start_code > end_code:
                raise FontParseError("cmap format 4 segment start exceeds end")
            for codepoint in range(start_code, end_code + 1):
                if codepoint == 0xFFFF:
                    continue
                if range_offset == 0:
                    glyph = (codepoint + delta) & 0xFFFF
                else:
                    range_word = id_range_offsets + 2 * segment
                    glyph_pos = range_word + range_offset + 2 * (codepoint - start_code)
                    if glyph_pos + 2 > offset + length:
                        raise FontParseError("cmap format 4 glyph index points outside subtable")
                    glyph = self._tu16(table, glyph_pos)
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                result[codepoint] = glyph
        return result

    def glyph_advance_1000(self, glyph_id: int) -> int:
        metrics = self.horizontal_metrics()
        if not 0 <= glyph_id < len(metrics):
            return 0
        return int(round(metrics[glyph_id][0] * 1000 / self.units_per_em))
