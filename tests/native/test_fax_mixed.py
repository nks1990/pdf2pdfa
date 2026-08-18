from __future__ import annotations

import pytest

from pdf2pdfa.native.ccitt import CCITTError
from pdf2pdfa.native.fax import decode_fax


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


EOL = "000000000001"


def test_k2_mixed_first_1d_then_2d_reference_line():
    # Row1 tag=1, H-like 1D coding -> white4 black4.
    # Row2 tag=0, identical reference via V0 at x4 then V0 at x8.
    encoded = _bits(
        f"{EOL} 1 1011 011 "
        f"{EOL} 0 1 1"
    )
    assert decode_fax(
        encoded,
        columns=8,
        rows=2,
        k=2,
        end_of_line=True,
    ) == b"\xf0\xf0"


def test_k3_allows_two_2d_rows_after_one_reference_row():
    encoded = _bits(
        f"{EOL} 1 1011 011 "
        f"{EOL} 0 1 1 "
        f"{EOL} 0 1 1"
    )
    assert decode_fax(
        encoded,
        columns=8,
        rows=3,
        k=3,
        end_of_line=True,
    ) == b"\xf0\xf0\xf0"


def test_mixed_new_1d_reference_resets_k_counter():
    encoded = _bits(
        f"{EOL} 1 1011 011 "       # WW WW BBBB
        f"{EOL} 0 1 1 "             # same via 2D
        f"{EOL} 1 1000 0010 "        # white3 black5
        f"{EOL} 0 1 1"               # same new reference via 2D
    )
    assert decode_fax(
        encoded,
        columns=8,
        rows=4,
        k=2,
        end_of_line=True,
    ) == b"\xf0\xf0\xe0\xe0"


def test_first_mixed_row_must_be_one_dimensional():
    encoded = _bits(f"{EOL} 0 1")
    with pytest.raises(CCITTError, match="first mixed Group 3 row"):
        decode_fax(encoded, columns=8, rows=1, k=2, end_of_line=True)


def test_k2_rejects_second_consecutive_2d_row():
    encoded = _bits(
        f"{EOL} 1 1011 011 "
        f"{EOL} 0 1 1 "
        f"{EOL} 0 1 1"
    )
    with pytest.raises(CCITTError, match="exceeds K=2"):
        decode_fax(encoded, columns=8, rows=3, k=2, end_of_line=True)


def test_mixed_tag_is_supported_even_when_eol_is_optional():
    # EndOfLine=false means EOL is not mandatory, not that MR loses its line
    # tag. A producer can omit EOL and provide tag+line data directly.
    encoded = _bits("1 1011 011 0 1 1")
    assert decode_fax(
        encoded,
        columns=8,
        rows=2,
        k=2,
        end_of_line=False,
    ) == b"\xf0\xf0"
