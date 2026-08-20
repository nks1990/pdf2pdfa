from __future__ import annotations

import pytest

from pdf2pdfa.native.ccitt import CCITTError, UnsupportedCCITTError, decode_ccitt


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    padding = (-len(clean)) % 8
    clean += "0" * padding
    return bytes(int(clean[index : index + 8], 2) for index in range(0, len(clean), 8))


def test_group3_1d_all_white_row():
    # White terminating run 8 = 10011.
    assert decode_ccitt(_bits("10011"), columns=8, rows=1, k=0) == b"\xff"


def test_group3_1d_black_row_uses_zero_white_run_then_black():
    # White run 0 + black run 8.
    encoded = _bits("00110101 000101")
    assert decode_ccitt(encoded, columns=8, rows=1, k=0) == b"\x00"


def test_group3_1d_alternating_runs_and_black_is_1():
    # White 4 + black 4.
    encoded = _bits("1011 011")
    assert decode_ccitt(encoded, columns=8, rows=1, k=0) == b"\xf0"
    assert decode_ccitt(
        encoded, columns=8, rows=1, k=0, black_is_1=True
    ) == b"\x0f"


def test_group3_makeup_plus_terminating_code():
    # White 70 = make-up 64 (11011) + terminating 6 (1110).
    decoded = decode_ccitt(_bits("11011 1110"), columns=70, rows=1, k=0)
    assert decoded == b"\xff" * 8 + b"\xfc"


def test_group3_eol_marker_is_consumed_when_declared():
    encoded = _bits("000000000001 10011")
    assert decode_ccitt(
        encoded,
        columns=8,
        rows=1,
        k=0,
        end_of_line=True,
    ) == b"\xff"


def test_group3_encoded_byte_alignment_between_rows():
    # Each white row uses five bits then is padded to the next byte boundary.
    encoded = _bits("10011 000 10011")
    assert decode_ccitt(
        encoded,
        columns=8,
        rows=2,
        k=0,
        encoded_byte_align=True,
    ) == b"\xff\xff"


def test_group4_first_all_white_row_is_vertical_zero():
    assert decode_ccitt(_bits("1"), columns=8, rows=1, k=-1) == b"\xff"


def test_group4_black_first_row_horizontal_then_identical_row_vertical():
    # Row 1: H, white0, black8. Row 2: V0 at x=0, V0 at x=8.
    encoded = _bits("001 00110101 000101 1 1")
    assert decode_ccitt(encoded, columns=8, rows=2, k=-1) == b"\x00\x00"


def test_group4_vertical_right_shift_uses_reference_changes():
    # Row 1 = WWWWBBBB: horizontal white4/black4.
    # Row 2 = WWWWWBBB: VR(1) moves first change from x=4 to x=5, then V0.
    encoded = _bits("001 1011 011 011 1")
    assert decode_ccitt(encoded, columns=8, rows=2, k=-1) == b"\xf0\xf8"


def test_group4_pass_mode_skips_reference_change_pair():
    # Row 1 = WWBBWWWW: H white2/black2, then V0 to the all-white reference edge.
    # Row 2 = all white: pass over reference b1=2,b2=4 then V0 at width.
    encoded = _bits("001 0111 11 1 0001 1")
    assert decode_ccitt(encoded, columns=8, rows=2, k=-1) == b"\xcf\xff"


def test_mixed_group3_remains_explicitly_unsupported():
    with pytest.raises(UnsupportedCCITTError, match="K > 0"):
        decode_ccitt(b"\x00", columns=8, rows=1, k=1)


def test_group4_extension_mode_is_fail_closed():
    with pytest.raises(UnsupportedCCITTError, match="extension"):
        decode_ccitt(_bits("0000001"), columns=8, rows=1, k=-1)


def test_truncated_huffman_code_fails():
    with pytest.raises(CCITTError):
        decode_ccitt(b"", columns=8, rows=1, k=0)


def test_run_exceeding_columns_is_rejected():
    # White terminating run 9 for a width-8 row.
    with pytest.raises(CCITTError, match="exceeds row width"):
        decode_ccitt(_bits("10100"), columns=8, rows=1, k=0)
