"""High-level PDF CCITT/T.4/T.6 row decoder.

``ccitt.py`` owns the Huffman/run and 2D primitives.  This module owns the PDF
line framing semantics around those primitives: optional/required EOL, byte
alignment, Modified READ tag bits, K cadence and Image-Height row limits.

The returned packed samples are canonical ordinary PDF bilevel samples:
``1 = white`` and ``0 = black``.  ``BlackIs1`` belongs to the source fax
filter convention and is normalized away before the filter is materialized.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ccitt import (
    CCITTError,
    UnsupportedCCITTError,
    _BitReader,
    _decode_1d_row,
    _decode_2d_row,
    _pack,
)


@dataclass(frozen=True, slots=True)
class FaxParameters:
    k: int = 0
    columns: int = 1728
    rows: int = 0
    end_of_line: bool = False
    encoded_byte_align: bool = False
    end_of_block: bool = True
    black_is_1: bool = False
    damaged_rows_before_error: int = 0

    def checked(self) -> "FaxParameters":
        if self.columns <= 0 or self.columns > 1_000_000:
            raise CCITTError(f"invalid CCITT Columns {self.columns}")
        if self.rows <= 0 or self.rows > 1_000_000:
            raise CCITTError(f"invalid CCITT Rows {self.rows}")
        if self.columns * self.rows > 250_000_000:
            raise CCITTError("unsafe CCITT decoded image dimensions")
        if self.k > 1_000_000:
            raise CCITTError(f"unreasonable CCITT K value {self.k}")
        if self.damaged_rows_before_error:
            raise UnsupportedCCITTError(
                "DamagedRowsBeforeError recovery is not implemented by the owned decoder"
            )
        return self


def _align_zero_fill(reader: _BitReader) -> None:
    """Advance to a byte boundary, requiring all skipped fill bits to be zero."""
    remainder = reader.position % 8
    if not remainder:
        return
    count = 8 - remainder
    for _ in range(count):
        if reader.read_bit() != 0:
            raise CCITTError("non-zero CCITT fill bit before byte boundary")


def _consume_optional_eol(reader: _BitReader, *, required: bool) -> bool:
    """Consume T.4 EOL plus any leading zero fill, or restore the bit position."""
    start = reader.position
    zeros = 0
    try:
        while zeros <= 64:
            bit = reader.read_bit()
            if bit:
                if zeros >= 11:
                    return True
                reader.position = start
                if required:
                    raise CCITTError("required CCITT EOL marker is absent")
                return False
            zeros += 1
    except CCITTError:
        reader.position = start
        if required:
            raise
        return False
    reader.position = start
    if required:
        raise CCITTError("required CCITT EOL marker is absent")
    return False


def _decode_mr_tagged_row(
    reader: _BitReader,
    *,
    reference: list[bool],
    columns: int,
    row_number: int,
    k: int,
    two_d_since_reference: int,
) -> tuple[list[bool], int]:
    """Decode one Modified READ line after its EOL, starting at the tag bit."""
    tag = reader.read_bit()
    if row_number == 1 and tag != 1:
        raise CCITTError("first mixed Group 3 row shall be one-dimensional")

    if tag == 1:
        row = _decode_1d_row(reader, columns)
        return row, 0

    # A positive K describes the maximum cycle: one one-dimensional reference
    # line followed by at most K-1 two-dimensional lines.
    if two_d_since_reference >= max(0, k - 1):
        raise CCITTError(
            f"mixed Group 3 exceeds K={k}: expected a one-dimensional reference row"
        )
    row = _decode_2d_row(reader, reference, columns)
    return row, two_d_since_reference + 1


def decode_fax(
    data: bytes,
    *,
    columns: int,
    rows: int,
    k: int = 0,
    end_of_line: bool = False,
    encoded_byte_align: bool = False,
    end_of_block: bool = True,
    black_is_1: bool = False,
    damaged_rows_before_error: int = 0,
) -> bytes:
    """Decode PDF CCITTFaxDecode bytes to canonical packed one-bit samples.

    ``black_is_1`` is parsed and retained in the parameter model for exact PDF
    semantics, but the materialized output is deliberately normalized to the
    ordinary PDF image convention (1 white / 0 black).  The caller may then
    remove ``/CCITTFaxDecode`` without needing an out-of-band polarity flag.
    """
    params = FaxParameters(
        k=k,
        columns=columns,
        rows=rows,
        end_of_line=end_of_line,
        encoded_byte_align=encoded_byte_align,
        end_of_block=end_of_block,
        black_is_1=black_is_1,
        damaged_rows_before_error=damaged_rows_before_error,
    ).checked()

    reader = _BitReader(data)
    decoded: list[list[bool]] = []
    reference = [False] * params.columns
    two_d_since_reference = 0

    for row_index in range(params.rows):
        row_number = row_index + 1

        # Fill is zeros between the preceding line data and the EOL sequence.
        # Skipping to a byte boundary first is safe because optional-EOL search
        # accepts the remaining zero fill plus the 12-bit EOL.  It also catches
        # non-zero padding instead of silently discarding it.
        if params.encoded_byte_align:
            _align_zero_fill(reader)

        eol_seen = _consume_optional_eol(reader, required=params.end_of_line)

        try:
            if params.k < 0:
                row = _decode_2d_row(reader, reference, params.columns)
            elif params.k == 0:
                row = _decode_1d_row(reader, params.columns)
            else:
                row, two_d_since_reference = _decode_mr_tagged_row(
                    reader,
                    reference=reference,
                    columns=params.columns,
                    row_number=row_number,
                    k=params.k,
                    two_d_since_reference=two_d_since_reference,
                )
        except (CCITTError, UnsupportedCCITTError) as exc:
            raise type(exc)(f"CCITT row {row_number}: {exc}") from exc

        decoded.append(row)
        reference = row

        # With byte-aligned T.4 and an EOL/tag prefix, the first data bit after
        # the prefix should land on a byte boundary.  We accept producer streams
        # that omit EOL when EndOfLine is false, but when an EOL is present we
        # require the declared byte-alignment contract to be true on the wire.
        if params.encoded_byte_align and eol_seen:
            # We cannot check this after row data has been consumed; the next
            # iteration validates its fill bits.  The comment documents why no
            # blind bit skipping occurs here.
            pass

    # EndOfBlock/RTC/EOFB is trailing framing.  The PDF Image /Height-derived
    # row count is authoritative for the pixels we need, so trailing framing is
    # neither required nor consumed here.
    return _pack(decoded, False)
