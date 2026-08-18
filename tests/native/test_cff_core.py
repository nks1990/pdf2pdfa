from __future__ import annotations

import pytest

from pdf2pdfa.native.cff import CFFError, CFFFont, UnsupportedCFFError


def _index(items: list[bytes]) -> bytes:
    if not items:
        return b"\x00\x00"
    offsets = [1]
    total = 1
    for item in items:
        total += len(item)
        offsets.append(total)
    off_size = 2
    return (
        len(items).to_bytes(2, "big")
        + bytes([off_size])
        + b"".join(value.to_bytes(off_size, "big") for value in offsets)
        + b"".join(items)
    )


def _dint(value: int) -> bytes:
    return b"\x1d" + int(value).to_bytes(4, "big", signed=True)


def _dict_matrix(values) -> bytes:
    return b"".join(_dint(value) for value in values) + b"\x0c\x07"


def _csnum(value: int) -> bytes:
    if -107 <= value <= 107:
        return bytes([value + 139])
    if -32768 <= value <= 32767:
        return b"\x1c" + value.to_bytes(2, "big", signed=True)
    raw = int(value * 65536)
    return b"\xff" + raw.to_bytes(4, "big", signed=True)


def _private(default_width: int, nominal_width: int, *, with_subrs: bool) -> bytes:
    # Subrs relative offset uses a fixed-width longint, so rebuilding with the
    # final private size cannot change the dictionary's own length.
    raw = b""
    if with_subrs:
        raw += _dint(0) + b"\x13"
    raw += _dint(default_width) + b"\x14"
    raw += _dint(nominal_width) + b"\x15"
    if with_subrs:
        raw = _dint(len(raw)) + b"\x13" + raw[6:]
    return raw


def _simple_cff(
    charstrings: list[bytes],
    *,
    charset_sids: list[int] | None = None,
    custom_strings: list[bytes] | None = None,
    local_subrs: list[bytes] | None = None,
    global_subrs: list[bytes] | None = None,
    default_width: int = 500,
    nominal_width: int = 0,
    font_matrix=None,
    charset_selector: int | None = None,
) -> bytes:
    if not charstrings:
        raise AssertionError("test CFF requires at least .notdef")
    charset_sids = charset_sids or list(range(len(charstrings)))
    custom_strings = custom_strings or []
    local_subrs = local_subrs or []
    global_subrs = global_subrs or []
    if len(charset_sids) != len(charstrings):
        raise AssertionError("charset/glyph count mismatch")

    header = b"\x01\x00\x04\x04"
    name = _index([b"OwnedTest"])
    strings = _index(custom_strings)
    gsubrs = _index(global_subrs)
    private = _private(default_width, nominal_width, with_subrs=bool(local_subrs))
    lsubrs = _index(local_subrs)
    charset = b"\x00" + b"".join(sid.to_bytes(2, "big") for sid in charset_sids[1:])
    chars = _index(charstrings)

    matrix_bytes = _dict_matrix(font_matrix) if font_matrix is not None else b""
    # Fixed-width placeholder offsets make top INDEX length deterministic.
    top_placeholder = (
        _dint(0) + b"\x0f"
        + _dint(0) + b"\x11"
        + _dint(len(private)) + _dint(0) + b"\x12"
        + matrix_bytes
    )
    prefix = header + name + _index([top_placeholder]) + strings + gsubrs
    charset_offset = len(prefix) if charset_selector is None else charset_selector
    private_offset = len(prefix) + (len(charset) if charset_selector is None else 0)
    charstrings_offset = private_offset + len(private) + len(lsubrs)

    top = (
        _dint(charset_offset) + b"\x0f"
        + _dint(charstrings_offset) + b"\x11"
        + _dint(len(private)) + _dint(private_offset) + b"\x12"
        + matrix_bytes
    )
    prefix = header + name + _index([top]) + strings + gsubrs
    assert len(prefix) == (private_offset if charset_selector is not None else len(prefix))
    body = b"" if charset_selector is not None else charset
    return prefix + body + private + lsubrs + chars


def _square(width: int = 500) -> bytes:
    del width
    return (
        _csnum(100) + _csnum(0) + b"\x15"  # rmoveto
        + _csnum(0) + _csnum(700) + b"\x05"
        + _csnum(500) + _csnum(0) + b"\x05"
        + _csnum(0) + _csnum(-700) + b"\x05"
        + _csnum(-500) + _csnum(0) + b"\x05"
        + b"\x0e"
    )


def test_simple_cff_iso_adobe_name_mapping_and_outline():
    font = CFFFont(_simple_cff([b"\x0e", _square()], charset_sids=[0, 34]))
    assert font.glyph_count == 2
    assert font.glyph_id_for_name("A") == 1
    outline = font.outline(1)
    assert outline.width == 500
    assert [command.operator for command in outline.commands] == ["M", "L", "L", "L", "L", "Z"]
    assert outline.commands[0].values == (100.0, 0.0)
    assert outline.commands[-2].values == (100.0, 0.0)


def test_explicit_width_uses_nominal_width_delta():
    program = _csnum(50) + _csnum(10) + _csnum(0) + b"\x15\x0e"
    font = CFFFont(
        _simple_cff(
            [b"\x0e", program],
            charset_sids=[0, 34],
            default_width=600,
            nominal_width=400,
        )
    )
    outline = font.outline(1)
    assert outline.width == 450
    assert outline.commands[0].values == (10.0, 0.0)


def test_local_subroutine_uses_bias_and_returns_to_parent_program():
    local = _csnum(0) + _csnum(700) + b"\x05\x0b"
    program = (
        _csnum(100) + _csnum(0) + b"\x15"
        + _csnum(-107) + b"\x0a"
        + b"\x0e"
    )
    font = CFFFont(
        _simple_cff(
            [b"\x0e", program],
            charset_sids=[0, 34],
            local_subrs=[local],
        )
    )
    outline = font.outline(1)
    assert [item.operator for item in outline.commands] == ["M", "L", "Z"]
    assert outline.commands[1].values == (100.0, 700.0)


def test_callgsubr_byte_29_is_operator_not_number():
    global_subr = _csnum(300) + _csnum(0) + b"\x05\x0b"
    program = (
        _csnum(100) + _csnum(0) + b"\x15"
        + _csnum(-107) + b"\x1d"  # callgsubr operator 29
        + b"\x0e"
    )
    font = CFFFont(
        _simple_cff(
            [b"\x0e", program],
            charset_sids=[0, 34],
            global_subrs=[global_subr],
        )
    )
    assert font.outline(1).commands[1].values == (400.0, 0.0)


def test_hvcurveto_byte_31_and_optional_fifth_delta():
    program = (
        _csnum(100) + _csnum(100) + b"\x15"
        + _csnum(50) + _csnum(20) + _csnum(30) + _csnum(40) + _csnum(10) + b"\x1f"
        + b"\x0e"
    )
    font = CFFFont(_simple_cff([b"\x0e", program], charset_sids=[0, 34]))
    curve = font.outline(1).commands[1]
    assert curve.operator == "C"
    assert curve.values == (150.0, 100.0, 170.0, 130.0, 180.0, 170.0)


def test_vhcurveto_byte_30_geometry():
    program = (
        _csnum(100) + _csnum(100) + b"\x15"
        + _csnum(40) + _csnum(20) + _csnum(30) + _csnum(50) + b"\x1e"
        + b"\x0e"
    )
    font = CFFFont(_simple_cff([b"\x0e", program], charset_sids=[0, 34]))
    assert font.outline(1).commands[1].values == (
        100.0, 140.0, 120.0, 170.0, 170.0, 170.0
    )


def test_hintmask_consumes_mask_bytes_not_charstring_operators():
    program = (
        _csnum(10) + _csnum(20) + b"\x01"  # one hstem
        + b"\x13\x80"                     # hintmask + one mask byte
        + _csnum(100) + _csnum(0) + b"\x15\x0e"
    )
    font = CFFFont(_simple_cff([b"\x0e", program], charset_sids=[0, 34]))
    assert font.outline(1).commands[0].values == (100.0, 0.0)


def test_deterministic_arithmetic_operator_can_feed_geometry():
    # 50 50 add -> 100, then 0, rmoveto.
    program = _csnum(50) + _csnum(50) + b"\x0c\x0a" + _csnum(0) + b"\x15\x0e"
    font = CFFFont(_simple_cff([b"\x0e", program], charset_sids=[0, 34]))
    assert font.outline(1).commands[0].values == (100.0, 0.0)


def test_random_operator_remains_explicitly_fail_closed():
    program = b"\x0c\x17" + _csnum(0) + b"\x15\x0e"
    font = CFFFont(_simple_cff([b"\x0e", program], charset_sids=[0, 34]))
    with pytest.raises(UnsupportedCFFError, match="random"):
        font.outline(1)


def test_custom_string_sid_maps_to_custom_glyph_name():
    font = CFFFont(
        _simple_cff(
            [b"\x0e", _square()],
            charset_sids=[0, 391],
            custom_strings=[b"ownedGlyph"],
        )
    )
    assert font.glyph_id_for_name("ownedGlyph") == 1


def test_expert_predefined_charset_is_fail_closed():
    font_data = _simple_cff(
        [b"\x0e", _square()],
        charset_sids=[0, 34],
        charset_selector=1,
    )
    with pytest.raises(UnsupportedCFFError, match="Expert"):
        CFFFont(font_data)


def test_missing_endchar_is_rejected():
    font = CFFFont(
        _simple_cff(
            [b"\x0e", _csnum(100) + _csnum(0) + b"\x15"],
            charset_sids=[0, 34],
        )
    )
    with pytest.raises(CFFError, match="without endchar"):
        font.outline(1)


def _cid_cff() -> bytes:
    header = b"\x01\x00\x04\x04"
    name = _index([b"OwnedCID"])
    strings = b"\x00\x00"
    gsubrs = b"\x00\x00"
    charset = b"\x00" + (100).to_bytes(2, "big")
    private = _private(700, 0, with_subrs=False)
    chars = _index([b"\x0e", _csnum(10) + _csnum(0) + b"\x15\x0e"])
    fdselect = b"\x00\x00\x00"  # format 0, two glyphs -> FD 0, FD 0

    fd_placeholder = _dint(len(private)) + _dint(0) + b"\x12" + _dict_matrix((2, 0, 0, 2, 0, 0))
    fd_index_placeholder = _index([fd_placeholder])
    top_placeholder = (
        _dint(0) + _dint(0) + _dint(0) + b"\x0c\x1e"  # ROS
        + _dint(0) + b"\x0f"                           # charset
        + _dint(0) + b"\x11"                           # CharStrings
        + _dint(0) + b"\x0c\x24"                      # FDArray
        + _dint(0) + b"\x0c\x25"                      # FDSelect
    )
    prefix = header + name + _index([top_placeholder]) + strings + gsubrs
    charset_offset = len(prefix)
    private_offset = charset_offset + len(charset)
    fdarray_offset = private_offset + len(private)
    fd_dict = _dint(len(private)) + _dint(private_offset) + b"\x12" + _dict_matrix((2, 0, 0, 2, 0, 0))
    fd_index = _index([fd_dict])
    assert len(fd_index) == len(fd_index_placeholder)
    fdselect_offset = fdarray_offset + len(fd_index)
    chars_offset = fdselect_offset + len(fdselect)

    top = (
        _dint(0) + _dint(0) + _dint(0) + b"\x0c\x1e"
        + _dint(charset_offset) + b"\x0f"
        + _dint(chars_offset) + b"\x11"
        + _dint(fdarray_offset) + b"\x0c\x24"
        + _dint(fdselect_offset) + b"\x0c\x25"
    )
    final_prefix = header + name + _index([top]) + strings + gsubrs
    assert len(final_prefix) == len(prefix)
    return final_prefix + charset + private + fd_index + fdselect + chars


def test_cid_keyed_cff_maps_cid_uses_fd_private_and_exposes_fd_matrix():
    font = CFFFont(_cid_cff())
    assert font.cid_keyed
    assert font.glyph_id_for_cid(100) == 1
    assert font.outline(1).width == 700
    assert font.fd_font_matrix(1) == (2.0, 0.0, 0.0, 2.0, 0.0, 0.0)
    with pytest.raises(CFFError, match="name lookup"):
        font.glyph_id_for_name("A")
