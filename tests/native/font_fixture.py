from __future__ import annotations

import struct


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * ((-len(data)) % 4)


def _head() -> bytes:
    data = bytearray(54)
    data[0:4] = (0x00010000).to_bytes(4, "big")
    data[4:8] = (0x00010000).to_bytes(4, "big")
    data[12:16] = (0x5F0F3CF5).to_bytes(4, "big")
    data[16:18] = (0).to_bytes(2, "big")
    data[18:20] = (1000).to_bytes(2, "big")
    # created/modified left zero for deterministic fixture
    data[36:38] = int(0).to_bytes(2, "big", signed=True)
    data[38:40] = int(-200).to_bytes(2, "big", signed=True)
    data[40:42] = int(600).to_bytes(2, "big", signed=True)
    data[42:44] = int(800).to_bytes(2, "big", signed=True)
    data[44:46] = (0).to_bytes(2, "big")
    data[46:48] = (8).to_bytes(2, "big")
    data[48:50] = int(2).to_bytes(2, "big", signed=True)
    data[50:52] = int(0).to_bytes(2, "big", signed=True)
    data[52:54] = int(0).to_bytes(2, "big", signed=True)
    return bytes(data)


def _hhea() -> bytes:
    data = bytearray(36)
    data[0:4] = (0x00010000).to_bytes(4, "big")
    data[4:6] = int(800).to_bytes(2, "big", signed=True)
    data[6:8] = int(-200).to_bytes(2, "big", signed=True)
    data[10:12] = (600).to_bytes(2, "big")
    data[34:36] = (2).to_bytes(2, "big")
    return bytes(data)


def _maxp() -> bytes:
    data = bytearray(32)
    data[0:4] = (0x00010000).to_bytes(4, "big")
    data[4:6] = (2).to_bytes(2, "big")
    return bytes(data)


def _hmtx() -> bytes:
    return (
        (600).to_bytes(2, "big")
        + int(0).to_bytes(2, "big", signed=True)
        + (600).to_bytes(2, "big")
        + int(0).to_bytes(2, "big", signed=True)
    )


def _os2(fs_type: int) -> bytes:
    data = bytearray(96)
    data[0:2] = (2).to_bytes(2, "big")
    data[2:4] = int(600).to_bytes(2, "big", signed=True)
    data[4:6] = (400).to_bytes(2, "big")
    data[6:8] = (5).to_bytes(2, "big")
    data[8:10] = fs_type.to_bytes(2, "big")
    data[68:70] = int(800).to_bytes(2, "big", signed=True)
    data[70:72] = int(-200).to_bytes(2, "big", signed=True)
    data[88:90] = int(700).to_bytes(2, "big", signed=True)
    data[90:92] = int(500).to_bytes(2, "big", signed=True)
    return bytes(data)


def _post() -> bytes:
    data = bytearray(32)
    data[0:4] = (0x00030000).to_bytes(4, "big")
    data[4:8] = (0).to_bytes(4, "big", signed=True)
    data[12:16] = (0).to_bytes(4, "big")
    return bytes(data)


def _name(postscript_name: str) -> bytes:
    records: list[tuple[int, int, int, int, bytes]] = []
    for name_id, value in (
        (1, "Owned Test Font"),
        (4, "Owned Test Font Regular"),
        (6, postscript_name),
    ):
        raw = value.encode("utf-16-be")
        records.append((3, 1, 0x0409, name_id, raw))
    count = len(records)
    string_offset = 6 + count * 12
    table = bytearray()
    table.extend((0).to_bytes(2, "big"))
    table.extend(count.to_bytes(2, "big"))
    table.extend(string_offset.to_bytes(2, "big"))
    strings = bytearray()
    for platform, encoding, language, name_id, raw in records:
        table.extend(platform.to_bytes(2, "big"))
        table.extend(encoding.to_bytes(2, "big"))
        table.extend(language.to_bytes(2, "big"))
        table.extend(name_id.to_bytes(2, "big"))
        table.extend(len(raw).to_bytes(2, "big"))
        table.extend(len(strings).to_bytes(2, "big"))
        strings.extend(raw)
    table.extend(strings)
    return bytes(table)


def _cmap() -> bytes:
    # Format 4 with segments for U+0041 -> glyph 1 and sentinel FFFF.
    seg_count = 2
    subtable = bytearray()
    subtable.extend((4).to_bytes(2, "big"))
    length = 16 + seg_count * 8
    subtable.extend(length.to_bytes(2, "big"))
    subtable.extend((0).to_bytes(2, "big"))  # language
    subtable.extend((seg_count * 2).to_bytes(2, "big"))
    subtable.extend((4).to_bytes(2, "big"))  # searchRange
    subtable.extend((1).to_bytes(2, "big"))  # entrySelector
    subtable.extend((0).to_bytes(2, "big"))  # rangeShift
    subtable.extend((0x0041).to_bytes(2, "big"))
    subtable.extend((0xFFFF).to_bytes(2, "big"))
    subtable.extend((0).to_bytes(2, "big"))  # reservedPad
    subtable.extend((0x0041).to_bytes(2, "big"))
    subtable.extend((0xFFFF).to_bytes(2, "big"))
    subtable.extend(((1 - 0x0041) & 0xFFFF).to_bytes(2, "big"))
    subtable.extend((1).to_bytes(2, "big"))  # FFFF -> glyph 0
    subtable.extend((0).to_bytes(2, "big"))
    subtable.extend((0).to_bytes(2, "big"))
    table = bytearray()
    table.extend((0).to_bytes(2, "big"))
    table.extend((1).to_bytes(2, "big"))
    table.extend((3).to_bytes(2, "big"))
    table.extend((1).to_bytes(2, "big"))
    table.extend((12).to_bytes(4, "big"))
    table.extend(subtable)
    return bytes(table)


def make_test_ttf(*, postscript_name: str = "OwnedTestFont", fs_type: int = 0) -> bytes:
    tables = {
        "OS/2": _os2(fs_type),
        "cmap": _cmap(),
        "glyf": b"\x00\x00\x00\x00",
        "head": _head(),
        "hhea": _hhea(),
        "hmtx": _hmtx(),
        "maxp": _maxp(),
        "name": _name(postscript_name),
        "post": _post(),
    }
    tags = sorted(tables)
    num_tables = len(tags)
    header = bytearray()
    header.extend(b"\x00\x01\x00\x00")
    header.extend(num_tables.to_bytes(2, "big"))
    # Search tuning fields are advisory for parsers; compute valid values.
    power = 1
    selector = 0
    while power * 2 <= num_tables:
        power *= 2
        selector += 1
    header.extend((power * 16).to_bytes(2, "big"))
    header.extend(selector.to_bytes(2, "big"))
    header.extend((num_tables * 16 - power * 16).to_bytes(2, "big"))

    directory = bytearray()
    payload = bytearray()
    offset = 12 + 16 * num_tables
    for tag in tags:
        raw = _pad4(tables[tag])
        directory.extend(tag.encode("latin-1"))
        directory.extend((0).to_bytes(4, "big"))  # checksum not needed by parser fixture
        directory.extend(offset.to_bytes(4, "big"))
        directory.extend(len(tables[tag]).to_bytes(4, "big"))
        payload.extend(raw)
        offset += len(raw)
    return bytes(header + directory + payload)
