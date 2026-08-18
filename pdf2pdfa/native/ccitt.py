"""Owned CCITT T.4/T.6 bi-level image decoder.

Supported PDF CCITTFaxDecode modes:

* ``K == 0``: one-dimensional Group 3 / Modified Huffman rows;
* ``K < 0``: pure two-dimensional Group 4 / T.6 rows.

Mixed Group 3 (``K > 0``), uncompressed extension mode and damaged-row
recovery remain fail-closed until their recovery/tag semantics are implemented
and tested.  The decoder returns packed one-bit samples in PDF image semantics:
by default 1 is white and 0 is black; ``BlackIs1`` reverses that mapping.
"""

from __future__ import annotations

from dataclasses import dataclass


class CCITTError(ValueError):
    pass


class UnsupportedCCITTError(CCITTError):
    pass


# T.4 terminating codes, run lengths 0..63.
_WHITE_TERMINATING = {
    "00110101": 0, "000111": 1, "0111": 2, "1000": 3,
    "1011": 4, "1100": 5, "1110": 6, "1111": 7,
    "10011": 8, "10100": 9, "00111": 10, "01000": 11,
    "001000": 12, "000011": 13, "110100": 14, "110101": 15,
    "101010": 16, "101011": 17, "0100111": 18, "0001100": 19,
    "0001000": 20, "0010111": 21, "0000011": 22, "0000100": 23,
    "0101000": 24, "0101011": 25, "0010011": 26, "0100100": 27,
    "0011000": 28, "00000010": 29, "00000011": 30, "00011010": 31,
    "00011011": 32, "00010010": 33, "00010011": 34, "00010100": 35,
    "00010101": 36, "00010110": 37, "00010111": 38, "00101000": 39,
    "00101001": 40, "00101010": 41, "00101011": 42, "00101100": 43,
    "00101101": 44, "00000100": 45, "00000101": 46, "00001010": 47,
    "00001011": 48, "01010010": 49, "01010011": 50, "01010100": 51,
    "01010101": 52, "00100100": 53, "00100101": 54, "01011000": 55,
    "01011001": 56, "01011010": 57, "01011011": 58, "01001010": 59,
    "01001011": 60, "00110010": 61, "00110011": 62, "00110100": 63,
}

_BLACK_TERMINATING = {
    "0000110111": 0, "010": 1, "11": 2, "10": 3,
    "011": 4, "0011": 5, "0010": 6, "00011": 7,
    "000101": 8, "000100": 9, "0000100": 10, "0000101": 11,
    "0000111": 12, "00000100": 13, "00000111": 14, "000011000": 15,
    "0000010111": 16, "0000011000": 17, "0000001000": 18, "00001100111": 19,
    "00001101000": 20, "00001101100": 21, "00000110111": 22, "00000101000": 23,
    "00000010111": 24, "00000011000": 25, "000011001010": 26, "000011001011": 27,
    "000011001100": 28, "000011001101": 29, "000001101000": 30, "000001101001": 31,
    "000001101010": 32, "000001101011": 33, "000011010010": 34, "000011010011": 35,
    "000011010100": 36, "000011010101": 37, "000011010110": 38, "000011010111": 39,
    "000001101100": 40, "000001101101": 41, "000011011010": 42, "000011011011": 43,
    "000001010100": 44, "000001010101": 45, "000001010110": 46, "000001010111": 47,
    "000001100100": 48, "000001100101": 49, "000001010010": 50, "000001010011": 51,
    "000000100100": 52, "000000110111": 53, "000000111000": 54, "000000100111": 55,
    "000000101000": 56, "000001011000": 57, "000001011001": 58, "000000101011": 59,
    "000000101100": 60, "000001011010": 61, "000001100110": 62, "000001100111": 63,
}

_WHITE_MAKEUP = {
    "11011": 64, "10010": 128, "010111": 192, "0110111": 256,
    "00110110": 320, "00110111": 384, "01100100": 448, "01100101": 512,
    "01101000": 576, "01100111": 640, "011001100": 704, "011001101": 768,
    "011010010": 832, "011010011": 896, "011010100": 960, "011010101": 1024,
    "011010110": 1088, "011010111": 1152, "011011000": 1216, "011011001": 1280,
    "011011010": 1344, "011011011": 1408, "010011000": 1472, "010011001": 1536,
    "010011010": 1600, "011000": 1664, "010011011": 1728,
}

_BLACK_MAKEUP = {
    "0000001111": 64, "000011001000": 128, "000011001001": 192,
    "000001011011": 256, "000000110011": 320, "000000110100": 384,
    "000000110101": 448, "0000001101100": 512, "0000001101101": 576,
    "0000001001010": 640, "0000001001011": 704, "0000001001100": 768,
    "0000001001101": 832, "0000001110010": 896, "0000001110011": 960,
    "0000001110100": 1024, "0000001110101": 1088, "0000001110110": 1152,
    "0000001110111": 1216, "0000001010010": 1280, "0000001010011": 1344,
    "0000001010100": 1408, "0000001010101": 1472, "0000001011010": 1536,
    "0000001011011": 1600, "0000001100100": 1664, "0000001100101": 1728,
}

_COMMON_MAKEUP = {
    "00000001000": 1792, "00000001100": 1856, "00000001101": 1920,
    "000000010010": 1984, "000000010011": 2048, "000000010100": 2112,
    "000000010101": 2176, "000000010110": 2240, "000000010111": 2304,
    "000000011100": 2368, "000000011101": 2432, "000000011110": 2496,
    "000000011111": 2560,
}

_2D_MODES = {
    "1": ("vertical", 0),
    "011": ("vertical", 1),
    "010": ("vertical", -1),
    "000011": ("vertical", 2),
    "000010": ("vertical", -2),
    "0000011": ("vertical", 3),
    "0000010": ("vertical", -3),
    "001": ("horizontal", 0),
    "0001": ("pass", 0),
    "0000001": ("extension", 0),
}


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) * 8 - self.position

    def read_bit(self) -> int:
        if self.position >= len(self.data) * 8:
            raise CCITTError("CCITT bitstream ended unexpectedly")
        byte = self.data[self.position // 8]
        shift = 7 - (self.position % 8)
        self.position += 1
        return (byte >> shift) & 1

    def align_byte(self) -> None:
        remainder = self.position % 8
        if remainder:
            self.position += 8 - remainder
        if self.position > len(self.data) * 8:
            raise CCITTError("CCITT byte alignment runs past end of stream")

    def read_eol(self) -> None:
        # T.4 EOL is 000000000001. Fill is permitted as additional zero bits,
        # so accept >= 11 zeros followed by one, but cap the search.
        zeros = 0
        while True:
            bit = self.read_bit()
            if bit == 1:
                if zeros < 11:
                    raise CCITTError("invalid CCITT EOL marker")
                return
            zeros += 1
            if zeros > 64:
                raise CCITTError("excessive fill before CCITT EOL")


@dataclass(frozen=True, slots=True)
class CCITTParameters:
    k: int = 0
    columns: int = 1728
    rows: int = 0
    end_of_line: bool = False
    encoded_byte_align: bool = False
    end_of_block: bool = True
    black_is_1: bool = False
    damaged_rows_before_error: int = 0

    def checked(self) -> "CCITTParameters":
        if self.columns <= 0 or self.columns > 1_000_000:
            raise CCITTError(f"invalid CCITT Columns {self.columns}")
        if self.rows < 0 or self.rows > 1_000_000:
            raise CCITTError(f"invalid CCITT Rows {self.rows}")
        if self.columns * max(1, self.rows) > 250_000_000:
            raise CCITTError("unsafe CCITT decoded image dimensions")
        if self.k > 0:
            raise UnsupportedCCITTError(
                "mixed Group 3 CCITT (K > 0) is not implemented by the owned decoder"
            )
        if self.damaged_rows_before_error:
            raise UnsupportedCCITTError(
                "DamagedRowsBeforeError recovery is not implemented by the owned decoder"
            )
        if self.k < 0 and self.end_of_line:
            raise UnsupportedCCITTError(
                "Group 4 with explicit EndOfLine markers is not supported"
            )
        return self


def _decode_code(reader: _BitReader, table: dict[str, int], *, max_bits: int = 13) -> int:
    bits = ""
    for _ in range(max_bits):
        bits += "1" if reader.read_bit() else "0"
        if bits in table:
            return table[bits]
    raise CCITTError(f"invalid CCITT Huffman code prefix {bits!r}")


def _decode_run(reader: _BitReader, black: bool) -> int:
    terminating = _BLACK_TERMINATING if black else _WHITE_TERMINATING
    makeup = dict(_BLACK_MAKEUP if black else _WHITE_MAKEUP)
    makeup.update(_COMMON_MAKEUP)
    total = 0
    segments = 0
    combined = dict(makeup)
    combined.update(terminating)
    while True:
        segments += 1
        if segments > 8192:
            raise CCITTError("excessive CCITT make-up sequence")
        value = _decode_code(reader, combined)
        total += value
        if total > 1_000_000_000:
            raise CCITTError("CCITT run length exceeds safety limit")
        # Terminating codes are exactly 0..63. Make-up codes are multiples of 64.
        if value < 64:
            return total


def _paint_run(row: list[bool], start: int, end: int, black: bool) -> None:
    if start < 0 or end < start or end > len(row):
        raise CCITTError(f"invalid CCITT run {start}..{end} for width {len(row)}")
    if black and end > start:
        row[start:end] = [True] * (end - start)


def _decode_1d_row(reader: _BitReader, columns: int) -> list[bool]:
    row = [False] * columns
    position = 0
    black = False
    runs = 0
    while position < columns:
        runs += 1
        if runs > columns * 2 + 1024:
            raise CCITTError("too many CCITT runs in one row")
        run = _decode_run(reader, black)
        end = position + run
        if end > columns:
            raise CCITTError(
                f"CCITT run exceeds row width ({position}+{run}>{columns})"
            )
        _paint_run(row, position, end, black)
        position = end
        black = not black
    return row


def _changes(row: list[bool]) -> list[int]:
    changes: list[int] = []
    previous = False  # imaginary white pixel before each row
    for index, value in enumerate(row):
        if value != previous:
            changes.append(index)
            previous = value
    # Two imaginary changes at the right edge guarantee b1/b2 sentinels.
    changes.extend((len(row), len(row)))
    return changes


def _b1_b2(reference: list[bool], a0: int, black: bool) -> tuple[int, int]:
    changes = _changes(reference)
    wanted = not black
    for index, position in enumerate(changes):
        # Starting from imaginary white, even change indices enter black and
        # odd indices enter white.
        new_black = index % 2 == 0
        if position >= a0 and new_black == wanted:
            b1 = position
            b2 = changes[index + 1] if index + 1 < len(changes) else len(reference)
            return b1, b2
    return len(reference), len(reference)


def _decode_2d_mode(reader: _BitReader) -> tuple[str, int]:
    bits = ""
    for _ in range(7):
        bits += "1" if reader.read_bit() else "0"
        mode = _2D_MODES.get(bits)
        if mode is not None:
            if mode[0] == "extension":
                raise UnsupportedCCITTError(
                    "CCITT uncompressed/extension mode is not implemented"
                )
            return mode
    raise CCITTError(f"invalid CCITT 2D mode prefix {bits!r}")


def _decode_2d_row(
    reader: _BitReader,
    reference: list[bool],
    columns: int,
) -> list[bool]:
    row = [False] * columns
    a0 = 0
    black = False
    operations = 0
    while a0 < columns:
        operations += 1
        if operations > columns * 4 + 1024:
            raise CCITTError("too many CCITT 2D operations in one row")
        mode, delta = _decode_2d_mode(reader)
        b1, b2 = _b1_b2(reference, a0, black)

        if mode == "pass":
            if b2 < a0 or b2 > columns:
                raise CCITTError("invalid CCITT pass-mode reference position")
            _paint_run(row, a0, b2, black)
            if b2 == a0:
                raise CCITTError("CCITT pass mode made no forward progress")
            a0 = b2
            continue

        if mode == "horizontal":
            first = _decode_run(reader, black)
            second = _decode_run(reader, not black)
            a1 = a0 + first
            a2 = a1 + second
            if a2 > columns:
                raise CCITTError("CCITT horizontal mode exceeds row width")
            _paint_run(row, a0, a1, black)
            _paint_run(row, a1, a2, not black)
            if a2 == a0:
                raise CCITTError("CCITT horizontal mode made no forward progress")
            a0 = a2
            # Two runs toggle twice, so coding color is unchanged.
            continue

        if mode == "vertical":
            a1 = b1 + delta
            if a1 < a0 or a1 > columns:
                raise CCITTError(
                    f"invalid CCITT vertical target {a1} from b1={b1}, delta={delta}"
                )
            _paint_run(row, a0, a1, black)
            if a1 == a0 and a0 == columns:
                break
            a0 = a1
            black = not black
            # A zero-length vertical transition is valid (for example a line
            # beginning black), but repeated zero progress is bounded by the
            # operation limit and the color change alters the next b1 search.
            continue

        raise CCITTError(f"unknown CCITT 2D mode {mode!r}")

    return row


def _pack(rows: list[list[bool]], black_is_1: bool) -> bytes:
    if not rows:
        return b""
    width = len(rows[0])
    row_bytes = (width + 7) // 8
    output = bytearray(row_bytes * len(rows))
    for y, row in enumerate(rows):
        if len(row) != width:
            raise CCITTError("inconsistent decoded CCITT row width")
        for x, black in enumerate(row):
            sample_one = black if black_is_1 else not black
            if sample_one:
                output[y * row_bytes + x // 8] |= 1 << (7 - (x % 8))
    return bytes(output)


def decode_ccitt(
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
    """Decode PDF CCITTFaxDecode bytes into packed one-bit image samples."""
    params = CCITTParameters(
        k=k,
        columns=columns,
        rows=rows,
        end_of_line=end_of_line,
        encoded_byte_align=encoded_byte_align,
        end_of_block=end_of_block,
        black_is_1=black_is_1,
        damaged_rows_before_error=damaged_rows_before_error,
    ).checked()
    if params.rows <= 0:
        raise CCITTError("owned CCITT decoder requires a positive row count")
    reader = _BitReader(data)
    decoded: list[list[bool]] = []

    if params.k == 0:
        for _ in range(params.rows):
            if params.end_of_line:
                reader.read_eol()
            row = _decode_1d_row(reader, params.columns)
            decoded.append(row)
            if params.encoded_byte_align:
                reader.align_byte()
    else:
        reference = [False] * params.columns
        for _ in range(params.rows):
            row = _decode_2d_row(reader, reference, params.columns)
            decoded.append(row)
            reference = row
            if params.encoded_byte_align:
                reader.align_byte()

    # EndOfBlock/RTC/EOFB occurs after the requested raster rows and is not
    # needed to reconstruct a PDF Image XObject whose Height is authoritative.
    # We intentionally do not consume arbitrary trailing bytes here.
    return _pack(decoded, params.black_is_1)
