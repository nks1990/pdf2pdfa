from __future__ import annotations

import pytest

from pdf2pdfa.native.type1 import Type1Error, UnsupportedType1Error
from pdf2pdfa.native.type1_flex import FlexSeacType1Font

from tests.native.test_type1_core import _notdef, _num, _pfa, _private


_CALLOTHERSUBR = b"\x0c\x10"
_POP = b"\x0c\x11"
_SETCURRENTPOINT = b"\x0c\x21"


def _othersubr(number: int, args: tuple[int, ...] = ()) -> bytes:
    return (
        b"".join(_num(value) for value in args)
        + _num(len(args))
        + _num(number)
        + _CALLOTHERSUBR
    )


def _flex_glyph(*, vectors=None, final_pops: bool = True) -> bytes:
    vectors = vectors or [
        (0, 0),
        (100, 0),
        (100, 100),
        (100, 0),
        (100, 0),
        (100, -100),
        (100, 0),
    ]
    out = [_num(0), _num(700), b"\x0d", _othersubr(1)]
    for dx, dy in vectors:
        out += [_num(dx), _num(dy), b"\x15", _othersubr(2)]
    out.append(_othersubr(0, (50, 0, 0)))
    if final_pops:
        out += [_POP, _POP, _SETCURRENTPOINT]
    out += [
        _num(0), _num(200), b"\x05",
        _num(-600), _num(0), b"\x05",
        b"\x09\x0e",
    ]
    return b"".join(out)


def _font(glyph: bytes) -> FlexSeacType1Font:
    return FlexSeacType1Font(
        _pfa(_private({".notdef": _notdef(), "A": glyph}))
    )


def test_standard_flex_builds_two_cubic_segments_from_samples_1_through_6():
    outline = _font(_flex_glyph()).outline("A")
    assert outline.width_x == 700
    assert [command.operator for command in outline.commands] == [
        "M", "C", "C", "L", "L", "Z"
    ]
    assert outline.commands[0].values == (0, 0)
    assert outline.commands[1].values == (100, 0, 200, 100, 300, 100)
    assert outline.commands[2].values == (400, 100, 500, 0, 600, 0)
    # pop/pop/setcurrentpoint leaves the CharString current point at the final
    # Flex point, so the following rlineto begins exactly there.
    assert outline.commands[3].values == (600, 200)
    assert outline.commands[4].values == (0, 200)


def test_hint_replacement_othersubr_3_returns_argument_through_pop():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _othersubr(3, (42,)), _POP,
            b"\x16",  # hmoveto consumes returned 42
            _num(100), _num(0), b"\x05",
            _num(-100), _num(100), b"\x05",
            b"\x09\x0e",
        ]
    )
    outline = _font(glyph).outline("A")
    assert outline.commands[0].values == (42, 0)


def test_counter_control_hint_othersubrs_do_not_change_geometry():
    glyph = b"".join(
        [
            _num(0), _num(600), b"\x0d",
            _othersubr(12, (10, 20)),
            _num(50), _num(50), b"\x15",
            _num(100), _num(0), b"\x05",
            b"\x0e",
        ]
    )
    outline = _font(glyph).outline("A")
    assert outline.commands[0].values == (50, 50)
    assert outline.commands[1].values == (150, 50)


def test_flex_vector_requires_preceding_move():
    glyph = b"".join(
        [_num(0), _num(600), b"\x0d", _othersubr(1), _othersubr(2), b"\x0e"]
    )
    with pytest.raises(Type1Error, match="requires a preceding move"):
        _font(glyph).outline("A")


def test_flex_end_requires_exactly_seven_vectors():
    with pytest.raises(Type1Error, match="exactly seven"):
        _font(_flex_glyph(vectors=[(0, 0)] * 6)).outline("A")


def test_eighth_flex_vector_is_rejected():
    with pytest.raises(Type1Error, match="more than seven"):
        _font(_flex_glyph(vectors=[(0, 0)] * 8)).outline("A")


def test_pop_without_pending_othersubr_result_is_rejected():
    glyph = _num(0) + _num(600) + b"\x0d" + _POP + b"\x0e"
    with pytest.raises(Type1Error, match="no pending OtherSubr result"):
        _font(glyph).outline("A")


def test_flex_result_protocol_requires_pop_pop_before_setcurrentpoint():
    with pytest.raises(Type1Error, match="setcurrentpoint requires all OtherSubr results"):
        glyph = _flex_glyph(final_pops=False).replace(
            _num(0) + _num(200) + b"\x05",
            _SETCURRENTPOINT + _num(0) + _num(200) + b"\x05",
            1,
        )
        _font(glyph).outline("A")


def test_unknown_othersubr_stays_fail_closed_without_postscript_vm():
    glyph = b"".join(
        [_num(0), _num(600), b"\x0d", _othersubr(20, (1, 2)), b"\x0e"]
    )
    with pytest.raises(UnsupportedType1Error, match="OtherSubr 20"):
        _font(glyph).outline("A")
