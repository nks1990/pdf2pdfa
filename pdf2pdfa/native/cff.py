"""Owned Compact Font Format (CFF1) and Type 2 CharString interpreter.

The module intentionally models the original CFF program instead of converting
it to another font format.  It supports the structures needed by PDF embedded
Type1C/CIDFontType0C programs: CFF1 INDEX/DICT, custom and ISOAdobe charsets,
Private DICT/local subroutines, CID FDArray/FDSelect and the Type 2 drawing,
subroutine, hint-consumption, flex and deterministic arithmetic operators used
by glyph programs.

Unsupported constructs fail closed (for example Expert predefined charsets,
``random`` and deprecated Type2 seac composition) rather than being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Iterable


class CFFError(ValueError):
    pass


class UnsupportedCFFError(CFFError):
    pass


MAX_INDEX_COUNT = 1_000_000
MAX_CHARSTRING_STACK = 48
MAX_SUBR_DEPTH = 32
MAX_CHARSTRING_OPS = 1_000_000
MAX_GLYPHS = 1_000_000


# CFF standard strings 0..228.  This covers ISOAdobe and the normal Latin name
# set used by the vast majority of PDF simple Type1C fonts. Higher Expert SID
# names remain explicit unsupported lookups until the owned table is extended.
_STANDARD_STRINGS_0_228 = tuple(
    """.notdef space exclam quotedbl numbersign dollar percent ampersand quoteright
parenleft parenright asterisk plus comma hyphen period slash zero one two three four
five six seven eight nine colon semicolon less equal greater question at A B C D E F
G H I J K L M N O P Q R S T U V W X Y Z bracketleft backslash bracketright
asciicircum underscore quoteleft a b c d e f g h i j k l m n o p q r s t u v w x y
z braceleft bar braceright asciitilde exclamdown cent sterling fraction yen florin
section currency quotesingle quotedblleft guillemotleft guilsinglleft guilsinglright
fi fl endash dagger daggerdbl periodcentered paragraph bullet quotesinglbase
quotedblbase quotedblright guillemotright ellipsis perthousand questiondown grave acute
circumflex tilde macron breve dotaccent dieresis ring cedilla hungarumlaut ogonek caron
emdash AE ordfeminine Lslash Oslash OE ordmasculine ae dotlessi lslash oslash oe
germandbls onesuperior logicalnot mu trademark Eth onehalf plusminus Thorn onequarter
divide brokenbar degree thorn threequarters twosuperior registered minus eth multiply
threesuperior copyright Aacute Acircumflex Adieresis Agrave Aring Atilde Ccedilla Eacute
Ecircumflex Edieresis Egrave Iacute Icircumflex Idieresis Igrave Ntilde Oacute
Ocircumflex Odieresis Ograve Otilde Scaron Uacute Ucircumflex Udieresis Ugrave Yacute
Ydieresis Zcaron aacute acircumflex adieresis agrave aring atilde ccedilla eacute
ecircumflex edieresis egrave iacute icircumflex idieresis igrave ntilde oacute
ocircumflex odieresis ograve otilde scaron uacute ucircumflex udieresis ugrave yacute
ydieresis zcaron""".split()
)


@dataclass(frozen=True, slots=True)
class CFFCommand:
    operator: str
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class CFFOutline:
    width: float
    commands: tuple[CFFCommand, ...]


@dataclass(frozen=True, slots=True)
class _PrivateData:
    default_width: float
    nominal_width: float
    local_subrs: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class _FDData:
    private: _PrivateData


class _Reader:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def require(self, count: int, label: str) -> None:
        if count < 0 or self.position + count > len(self.data):
            raise CFFError(f"truncated CFF while reading {label}")

    def u8(self, label: str) -> int:
        self.require(1, label)
        value = self.data[self.position]
        self.position += 1
        return value

    def u16(self, label: str) -> int:
        self.require(2, label)
        value = int.from_bytes(self.data[self.position : self.position + 2], "big")
        self.position += 2
        return value

    def bytes(self, count: int, label: str) -> bytes:
        self.require(count, label)
        value = self.data[self.position : self.position + count]
        self.position += count
        return value


def _read_offset(reader: _Reader, size: int, label: str) -> int:
    if size not in (1, 2, 3, 4):
        raise CFFError(f"invalid CFF offSize {size}")
    return int.from_bytes(reader.bytes(size, label), "big")


def _read_index(data: bytes, offset: int, label: str) -> tuple[tuple[bytes, ...], int]:
    reader = _Reader(data, offset)
    count = reader.u16(f"{label} count")
    if count > MAX_INDEX_COUNT:
        raise CFFError(f"{label} count {count} exceeds owned limit")
    if count == 0:
        return (), reader.position
    off_size = reader.u8(f"{label} offSize")
    offsets = [
        _read_offset(reader, off_size, f"{label} offset")
        for _ in range(count + 1)
    ]
    if offsets[0] != 1:
        raise CFFError(f"{label} first offset shall be 1")
    if any(value <= 0 for value in offsets):
        raise CFFError(f"{label} contains a non-positive offset")
    if any(right < left for left, right in zip(offsets, offsets[1:])):
        raise CFFError(f"{label} offsets are not monotonic")
    data_start = reader.position
    end = data_start + offsets[-1] - 1
    if end > len(data):
        raise CFFError(f"{label} data extends past end of CFF")
    items = tuple(
        data[data_start + offsets[index] - 1 : data_start + offsets[index + 1] - 1]
        for index in range(count)
    )
    return items, end


def _dict_number(data: bytes, position: int) -> tuple[float, int]:
    if position >= len(data):
        raise CFFError("truncated CFF DICT operand")
    b0 = data[position]
    position += 1
    if 32 <= b0 <= 246:
        return float(b0 - 139), position
    if 247 <= b0 <= 250:
        if position >= len(data):
            raise CFFError("truncated positive CFF DICT operand")
        value = (b0 - 247) * 256 + data[position] + 108
        return float(value), position + 1
    if 251 <= b0 <= 254:
        if position >= len(data):
            raise CFFError("truncated negative CFF DICT operand")
        value = -(b0 - 251) * 256 - data[position] - 108
        return float(value), position + 1
    if b0 == 28:
        if position + 2 > len(data):
            raise CFFError("truncated CFF DICT shortint")
        return float(int.from_bytes(data[position : position + 2], "big", signed=True)), position + 2
    if b0 == 29:
        if position + 4 > len(data):
            raise CFFError("truncated CFF DICT longint")
        return float(int.from_bytes(data[position : position + 4], "big", signed=True)), position + 4
    if b0 == 30:
        chars: list[str] = []
        done = False
        while not done:
            if position >= len(data):
                raise CFFError("truncated CFF DICT real")
            byte = data[position]
            position += 1
            for nibble in (byte >> 4, byte & 0x0F):
                if nibble <= 9:
                    chars.append(str(nibble))
                elif nibble == 0xA:
                    chars.append(".")
                elif nibble == 0xB:
                    chars.append("E")
                elif nibble == 0xC:
                    chars.append("E-")
                elif nibble == 0xE:
                    chars.append("-")
                elif nibble == 0xF:
                    done = True
                    break
                else:
                    raise CFFError("reserved nibble in CFF DICT real")
        text = "".join(chars)
        try:
            value = float(text)
        except ValueError as exc:
            raise CFFError(f"invalid CFF DICT real {text!r}") from exc
        if not math.isfinite(value):
            raise CFFError("non-finite CFF DICT real")
        return value, position
    raise CFFError(f"byte {b0} is not a CFF DICT operand")


def _parse_dict(data: bytes) -> dict[tuple[int, ...], tuple[float, ...]]:
    result: dict[tuple[int, ...], tuple[float, ...]] = {}
    stack: list[float] = []
    position = 0
    while position < len(data):
        b0 = data[position]
        if b0 >= 28 or b0 == 30:
            value, position = _dict_number(data, position)
            stack.append(value)
            if len(stack) > 48:
                raise CFFError("CFF DICT operand stack exceeds 48")
            continue
        position += 1
        if b0 == 12:
            if position >= len(data):
                raise CFFError("truncated escaped CFF DICT operator")
            operator = (12, data[position])
            position += 1
        elif 0 <= b0 <= 21:
            operator = (b0,)
        else:
            raise CFFError(f"reserved CFF DICT operator byte {b0}")
        result[operator] = tuple(stack)
        stack.clear()
    if stack:
        raise CFFError("CFF DICT ends with unused operands")
    return result


def _int_operand(values: tuple[float, ...] | None, label: str, count: int = 1) -> tuple[int, ...]:
    if values is None or len(values) != count:
        raise CFFError(f"{label} requires {count} integer operand(s)")
    output: list[int] = []
    for value in values:
        integer = int(value)
        if integer != value:
            raise CFFError(f"{label} contains a non-integer operand")
        output.append(integer)
    return tuple(output)


def _subr_bias(count: int) -> int:
    if count < 1240:
        return 107
    if count < 33900:
        return 1131
    return 32768


def _read_charset(data: bytes, offset: int, glyph_count: int) -> tuple[int, ...]:
    if glyph_count <= 0 or glyph_count > MAX_GLYPHS:
        raise CFFError(f"invalid CFF glyph count {glyph_count}")
    if offset == 0:
        # ISOAdobe's GID order is SID 0..228.
        if glyph_count > 229:
            raise UnsupportedCFFError(
                "ISOAdobe predefined charset cannot contain more than 229 glyphs"
            )
        return tuple(range(glyph_count))
    if offset in (1, 2):
        raise UnsupportedCFFError(
            "Expert/ExpertSubset predefined CFF charsets are not yet implemented"
        )
    reader = _Reader(data, offset)
    fmt = reader.u8("CFF charset format")
    values = [0]
    if fmt == 0:
        while len(values) < glyph_count:
            values.append(reader.u16("CFF charset SID/CID"))
    elif fmt in (1, 2):
        while len(values) < glyph_count:
            first = reader.u16("CFF charset range first")
            n_left = reader.u8("CFF charset nLeft") if fmt == 1 else reader.u16("CFF charset nLeft")
            if len(values) + n_left + 1 > glyph_count:
                raise CFFError("CFF charset range exceeds glyph count")
            values.extend(first + delta for delta in range(n_left + 1))
    else:
        raise CFFError(f"unsupported CFF charset format {fmt}")
    return tuple(values)


def _read_fdselect(data: bytes, offset: int, glyph_count: int, fd_count: int) -> tuple[int, ...]:
    reader = _Reader(data, offset)
    fmt = reader.u8("CFF FDSelect format")
    if fmt == 0:
        values = tuple(reader.u8("CFF FDSelect entry") for _ in range(glyph_count))
    elif fmt == 3:
        range_count = reader.u16("CFF FDSelect range count")
        ranges: list[tuple[int, int]] = []
        previous = -1
        for _ in range(range_count):
            first = reader.u16("CFF FDSelect first glyph")
            fd = reader.u8("CFF FDSelect fd")
            if first <= previous:
                raise CFFError("CFF FDSelect ranges are not strictly increasing")
            previous = first
            ranges.append((first, fd))
        sentinel = reader.u16("CFF FDSelect sentinel")
        if not ranges or ranges[0][0] != 0 or sentinel != glyph_count:
            raise CFFError("CFF FDSelect format 3 has invalid first/sentinel glyph")
        output = [0] * glyph_count
        for index, (first, fd) in enumerate(ranges):
            stop = ranges[index + 1][0] if index + 1 < len(ranges) else sentinel
            if stop > glyph_count:
                raise CFFError("CFF FDSelect range exceeds glyph count")
            output[first:stop] = [fd] * (stop - first)
        values = tuple(output)
    else:
        raise UnsupportedCFFError(f"CFF1 FDSelect format {fmt} is not implemented")
    if any(value >= fd_count for value in values):
        raise CFFError("CFF FDSelect references a missing Font DICT")
    return values


def _private_data(data: bytes, values: tuple[float, ...] | None) -> _PrivateData:
    if values is None:
        return _PrivateData(0.0, 0.0, ())
    size, offset = _int_operand(values, "CFF Private", 2)
    if size < 0 or offset < 0 or offset + size > len(data):
        raise CFFError("CFF Private DICT lies outside font data")
    private_dict = _parse_dict(data[offset : offset + size])
    default_width = private_dict.get((20,), (0.0,))
    nominal_width = private_dict.get((21,), (0.0,))
    if len(default_width) != 1 or len(nominal_width) != 1:
        raise CFFError("CFF private width operators require one operand")
    subrs: tuple[bytes, ...] = ()
    if (19,) in private_dict:
        relative, = _int_operand(private_dict[(19,)], "CFF Subrs")
        subr_offset = offset + relative
        subrs, _ = _read_index(data, subr_offset, "CFF Local Subr INDEX")
    return _PrivateData(default_width[0], nominal_width[0], subrs)


def _sid_name(sid: int, custom_strings: tuple[bytes, ...]) -> str:
    if 0 <= sid < len(_STANDARD_STRINGS_0_228):
        return _STANDARD_STRINGS_0_228[sid]
    if sid < 391:
        raise UnsupportedCFFError(
            f"CFF standard Expert SID {sid} name table is not yet owned"
        )
    index = sid - 391
    if index < 0 or index >= len(custom_strings):
        raise CFFError(f"CFF SID {sid} references a missing String INDEX entry")
    try:
        return custom_strings[index].decode("ascii")
    except UnicodeDecodeError as exc:
        raise CFFError(f"CFF custom SID {sid} is not ASCII") from exc


class _Type2Interpreter:
    def __init__(
        self,
        charstring: bytes,
        *,
        global_subrs: tuple[bytes, ...],
        private: _PrivateData,
    ) -> None:
        self.charstring = charstring
        self.global_subrs = global_subrs
        self.private = private
        self.stack: list[float] = []
        self.transient = [0.0] * 32
        self.x = 0.0
        self.y = 0.0
        self.commands: list[CFFCommand] = []
        self.open_contour = False
        self.stem_count = 0
        self.width: float | None = None
        self.ops = 0
        self.ended = False

    def _push(self, value: float) -> None:
        if not math.isfinite(value):
            raise CFFError("Type2 CharString produced a non-finite number")
        self.stack.append(float(value))
        if len(self.stack) > MAX_CHARSTRING_STACK:
            raise CFFError("Type2 operand stack exceeds 48")

    def _pop(self, label: str) -> float:
        if not self.stack:
            raise CFFError(f"Type2 {label} requires an operand")
        return self.stack.pop()

    def _close(self) -> None:
        if self.open_contour:
            self.commands.append(CFFCommand("Z", ()))
            self.open_contour = False

    def _move(self, dx: float, dy: float) -> None:
        self._close()
        self.x += dx
        self.y += dy
        self.commands.append(CFFCommand("M", (self.x, self.y)))
        self.open_contour = True

    def _line(self, dx: float, dy: float) -> None:
        if not self.open_contour:
            raise CFFError("Type2 line operator used before moveto")
        self.x += dx
        self.y += dy
        self.commands.append(CFFCommand("L", (self.x, self.y)))

    def _curve(self, values: Iterable[float]) -> None:
        numbers = tuple(values)
        if len(numbers) != 6:
            raise CFFError("Type2 curve requires six deltas")
        if not self.open_contour:
            raise CFFError("Type2 curve operator used before moveto")
        dx1, dy1, dx2, dy2, dx3, dy3 = numbers
        x1, y1 = self.x + dx1, self.y + dy1
        x2, y2 = x1 + dx2, y1 + dy2
        self.x, self.y = x2 + dx3, y2 + dy3
        self.commands.append(CFFCommand("C", (x1, y1, x2, y2, self.x, self.y)))

    def _take_width_stem(self) -> None:
        if self.width is None and len(self.stack) % 2:
            self.width = self.private.nominal_width + self.stack.pop(0)

    def _take_width_move(self, required: int) -> None:
        if self.width is None and len(self.stack) > required:
            if len(self.stack) != required + 1:
                raise CFFError("Type2 moveto has an invalid operand count")
            self.width = self.private.nominal_width + self.stack.pop(0)

    def _stem(self) -> None:
        self._take_width_stem()
        if len(self.stack) % 2:
            raise CFFError("Type2 stem operator requires operand pairs")
        self.stem_count += len(self.stack) // 2
        self.stack.clear()

    def _call_subr(self, global_subr: bool, depth: int) -> None:
        raw = int(self._pop("callsubr"))
        source = self.global_subrs if global_subr else self.private.local_subrs
        index = raw + _subr_bias(len(source))
        if index < 0 or index >= len(source):
            raise CFFError(f"Type2 subroutine index {index} is out of range")
        if depth >= MAX_SUBR_DEPTH:
            raise CFFError("Type2 subroutine recursion exceeds owned limit")
        returned = self._execute(source[index], depth + 1, is_subr=True)
        if not returned:
            raise CFFError("Type2 subroutine ended without return")

    def _escaped(self, operator: int) -> None:
        s = self.stack
        if operator == 3:  # and
            b, a = self._pop("and"), self._pop("and")
            self._push(1.0 if a and b else 0.0)
        elif operator == 4:  # or
            b, a = self._pop("or"), self._pop("or")
            self._push(1.0 if a or b else 0.0)
        elif operator == 5:  # not
            self._push(0.0 if self._pop("not") else 1.0)
        elif operator == 9:  # abs
            self._push(abs(self._pop("abs")))
        elif operator == 10:  # add
            b, a = self._pop("add"), self._pop("add")
            self._push(a + b)
        elif operator == 11:  # sub
            b, a = self._pop("sub"), self._pop("sub")
            self._push(a - b)
        elif operator == 12:  # div
            b, a = self._pop("div"), self._pop("div")
            if b == 0:
                raise CFFError("Type2 division by zero")
            self._push(a / b)
        elif operator == 14:  # neg
            self._push(-self._pop("neg"))
        elif operator == 15:  # eq
            b, a = self._pop("eq"), self._pop("eq")
            self._push(1.0 if a == b else 0.0)
        elif operator == 18:  # drop
            self._pop("drop")
        elif operator == 20:  # put
            index = int(self._pop("put"))
            value = self._pop("put")
            if not 0 <= index < len(self.transient):
                raise CFFError("Type2 put index is out of range")
            self.transient[index] = value
        elif operator == 21:  # get
            index = int(self._pop("get"))
            if not 0 <= index < len(self.transient):
                raise CFFError("Type2 get index is out of range")
            self._push(self.transient[index])
        elif operator == 22:  # ifelse
            v2 = self._pop("ifelse")
            v1 = self._pop("ifelse")
            s2 = self._pop("ifelse")
            s1 = self._pop("ifelse")
            self._push(s1 if v1 <= v2 else s2)
        elif operator == 23:
            raise UnsupportedCFFError("Type2 random operator is intentionally fail-closed")
        elif operator == 24:  # mul
            b, a = self._pop("mul"), self._pop("mul")
            self._push(a * b)
        elif operator == 26:  # sqrt
            value = self._pop("sqrt")
            if value < 0:
                raise CFFError("Type2 sqrt of negative value")
            self._push(math.sqrt(value))
        elif operator == 27:  # dup
            value = self._pop("dup")
            self._push(value)
            self._push(value)
        elif operator == 28:  # exch
            if len(s) < 2:
                raise CFFError("Type2 exch requires two operands")
            s[-1], s[-2] = s[-2], s[-1]
        elif operator == 29:  # index
            index = int(self._pop("index"))
            if not s:
                raise CFFError("Type2 index requires a source operand")
            index = max(0, min(index, len(s) - 1))
            self._push(s[-1 - index])
        elif operator == 30:  # roll
            j = int(self._pop("roll"))
            n = int(self._pop("roll"))
            if n < 0 or n > len(s):
                raise CFFError("Type2 roll has invalid n")
            if n:
                j %= n
                tail = s[-n:]
                s[-n:] = tail[-j:] + tail[:-j]
        elif operator == 34:  # hflex
            if len(s) != 7:
                raise CFFError("Type2 hflex requires seven operands")
            dx1, dx2, dy2, dx3, dx4, dx5, dx6 = s
            self._curve((dx1, 0, dx2, dy2, dx3, 0))
            self._curve((dx4, 0, dx5, -dy2, dx6, 0))
            s.clear()
        elif operator == 35:  # flex
            if len(s) != 13:
                raise CFFError("Type2 flex requires thirteen operands")
            self._curve(s[0:6])
            self._curve(s[6:12])
            s.clear()  # flex depth is ignored for outline geometry
        elif operator == 36:  # hflex1
            if len(s) != 9:
                raise CFFError("Type2 hflex1 requires nine operands")
            dx1, dy1, dx2, dy2, dx3, dx4, dx5, dy5, dx6 = s
            self._curve((dx1, dy1, dx2, dy2, dx3, 0))
            self._curve((dx4, 0, dx5, dy5, dx6, -(dy1 + dy2 + dy5)))
            s.clear()
        elif operator == 37:  # flex1
            if len(s) != 11:
                raise CFFError("Type2 flex1 requires eleven operands")
            first = s[:10]
            sum_x = first[0] + first[2] + first[4] + first[6] + first[8]
            sum_y = first[1] + first[3] + first[5] + first[7] + first[9]
            final = s[10]
            if abs(sum_x) > abs(sum_y):
                dx6, dy6 = final, -sum_y
            else:
                dx6, dy6 = -sum_x, final
            self._curve(first[:6])
            self._curve((first[6], first[7], first[8], first[9], dx6, dy6))
            s.clear()
        else:
            raise UnsupportedCFFError(f"unsupported escaped Type2 operator 12 {operator}")

    def _number(self, data: bytes, position: int) -> tuple[float, int]:
        b0 = data[position]
        position += 1
        if 32 <= b0 <= 246:
            return float(b0 - 139), position
        if 247 <= b0 <= 250:
            if position >= len(data):
                raise CFFError("truncated positive Type2 number")
            return float((b0 - 247) * 256 + data[position] + 108), position + 1
        if 251 <= b0 <= 254:
            if position >= len(data):
                raise CFFError("truncated negative Type2 number")
            return float(-(b0 - 251) * 256 - data[position] - 108), position + 1
        if b0 == 28:
            if position + 2 > len(data):
                raise CFFError("truncated Type2 shortint")
            return float(int.from_bytes(data[position : position + 2], "big", signed=True)), position + 2
        if b0 == 255:
            if position + 4 > len(data):
                raise CFFError("truncated Type2 16.16 number")
            raw = int.from_bytes(data[position : position + 4], "big", signed=True)
            return raw / 65536.0, position + 4
        raise CFFError(f"byte {b0} is not a Type2 number")

    def _execute(self, data: bytes, depth: int, *, is_subr: bool) -> bool:
        position = 0
        while position < len(data):
            self.ops += 1
            if self.ops > MAX_CHARSTRING_OPS:
                raise CFFError("Type2 operator count exceeds owned limit")
            b0 = data[position]
            if b0 >= 28 or b0 == 255:
                value, position = self._number(data, position)
                self._push(value)
                continue
            position += 1

            if b0 in (1, 3, 18, 23):
                self._stem()
            elif b0 in (19, 20):
                self._stem()
                mask_bytes = (self.stem_count + 7) // 8
                if position + mask_bytes > len(data):
                    raise CFFError("truncated Type2 hint mask")
                position += mask_bytes
            elif b0 == 4:
                self._take_width_move(1)
                if len(self.stack) != 1:
                    raise CFFError("Type2 vmoveto requires one operand")
                self._move(0, self.stack[0])
                self.stack.clear()
            elif b0 == 5:
                if not self.stack or len(self.stack) % 2:
                    raise CFFError("Type2 rlineto requires dx/dy pairs")
                for index in range(0, len(self.stack), 2):
                    self._line(self.stack[index], self.stack[index + 1])
                self.stack.clear()
            elif b0 in (6, 7):
                if not self.stack:
                    raise CFFError("Type2 hlineto/vlineto requires operands")
                horizontal = b0 == 6
                for value in self.stack:
                    self._line(value if horizontal else 0, 0 if horizontal else value)
                    horizontal = not horizontal
                self.stack.clear()
            elif b0 == 8:
                if not self.stack or len(self.stack) % 6:
                    raise CFFError("Type2 rrcurveto requires groups of six")
                for index in range(0, len(self.stack), 6):
                    self._curve(self.stack[index : index + 6])
                self.stack.clear()
            elif b0 == 10:
                self._call_subr(False, depth)
            elif b0 == 11:
                if not is_subr:
                    raise CFFError("Type2 return outside subroutine")
                return True
            elif b0 == 12:
                if position >= len(data):
                    raise CFFError("truncated escaped Type2 operator")
                operator = data[position]
                position += 1
                self._escaped(operator)
            elif b0 == 14:
                if self.width is None and len(self.stack) in (1, 5):
                    self.width = self.private.nominal_width + self.stack.pop(0)
                if len(self.stack) == 4:
                    raise UnsupportedCFFError(
                        "deprecated Type2 endchar seac composition is not yet implemented"
                    )
                if self.stack:
                    raise CFFError("Type2 endchar has unexpected operands")
                self._close()
                self.ended = True
                return False
            elif b0 == 21:
                self._take_width_move(2)
                if len(self.stack) != 2:
                    raise CFFError("Type2 rmoveto requires two operands")
                self._move(self.stack[0], self.stack[1])
                self.stack.clear()
            elif b0 == 22:
                self._take_width_move(1)
                if len(self.stack) != 1:
                    raise CFFError("Type2 hmoveto requires one operand")
                self._move(self.stack[0], 0)
                self.stack.clear()
            elif b0 == 24:  # rcurveline
                if len(self.stack) < 8 or (len(self.stack) - 2) % 6:
                    raise CFFError("Type2 rcurveline has invalid operand count")
                curve_end = len(self.stack) - 2
                for index in range(0, curve_end, 6):
                    self._curve(self.stack[index : index + 6])
                self._line(self.stack[-2], self.stack[-1])
                self.stack.clear()
            elif b0 == 25:  # rlinecurve
                if len(self.stack) < 8 or (len(self.stack) - 6) % 2:
                    raise CFFError("Type2 rlinecurve has invalid operand count")
                line_end = len(self.stack) - 6
                for index in range(0, line_end, 2):
                    self._line(self.stack[index], self.stack[index + 1])
                self._curve(self.stack[-6:])
                self.stack.clear()
            elif b0 in (26, 27):  # vvcurveto / hhcurveto
                horizontal = b0 == 27
                values = list(self.stack)
                self.stack.clear()
                if len(values) % 4 == 1:
                    extra = values.pop(0)
                else:
                    extra = 0.0
                if not values or len(values) % 4:
                    raise CFFError("Type2 hh/vvcurveto has invalid operand count")
                first = True
                for index in range(0, len(values), 4):
                    a, b, c, d = values[index : index + 4]
                    if horizontal:
                        self._curve((a, extra if first else 0, b, c, d, 0))
                    else:
                        self._curve((extra if first else 0, a, b, c, 0, d))
                    first = False
            elif b0 in (30, 31):  # vhcurveto / hvcurveto
                values = list(self.stack)
                self.stack.clear()
                if len(values) < 4:
                    raise CFFError("Type2 hv/vhcurveto requires at least four operands")
                horizontal_first = b0 == 31
                index = 0
                horizontal = horizontal_first
                while len(values) - index >= 4:
                    remaining = len(values) - index
                    a, b, c, d = values[index : index + 4]
                    index += 4
                    final_extra = 0.0
                    if remaining == 5:
                        final_extra = values[index]
                        index += 1
                    if horizontal:
                        self._curve((a, 0, b, c, final_extra if remaining == 5 else d, d if remaining == 5 else 0))
                    else:
                        self._curve((0, a, b, c, d if remaining != 5 else 0, final_extra if remaining == 5 else d))
                    horizontal = not horizontal
                if index != len(values):
                    raise CFFError("Type2 hv/vhcurveto has invalid trailing operands")
            elif b0 == 29:
                self._call_subr(True, depth)
            else:
                raise UnsupportedCFFError(f"unsupported Type2 operator {b0}")
        if is_subr:
            return False
        raise CFFError("Type2 CharString ended without endchar")

    def run(self) -> CFFOutline:
        self._execute(self.charstring, 0, is_subr=False)
        if not self.ended:
            raise CFFError("Type2 CharString did not execute endchar")
        width = self.private.default_width if self.width is None else self.width
        return CFFOutline(width=width, commands=tuple(self.commands))


class CFFFont:
    """Parse one CFF1 font program and expose glyph outlines/mapping."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 4:
            raise CFFError("CFF data is shorter than its header")
        major, minor, header_size, off_size = data[:4]
        if major != 1:
            raise UnsupportedCFFError(f"only CFF1 is supported, got {major}.{minor}")
        if header_size < 4 or header_size > len(data):
            raise CFFError("invalid CFF header size")
        if off_size not in (1, 2, 3, 4):
            raise CFFError("invalid CFF header offSize")
        self.data = data

        names, position = _read_index(data, header_size, "CFF Name INDEX")
        top_dicts, position = _read_index(data, position, "CFF Top DICT INDEX")
        strings, position = _read_index(data, position, "CFF String INDEX")
        global_subrs, position = _read_index(data, position, "CFF Global Subr INDEX")
        del position
        if len(names) != 1 or len(top_dicts) != 1:
            raise UnsupportedCFFError(
                "owned PDF CFF parser currently requires exactly one font in the CFF set"
            )
        self.name = names[0].decode("ascii", "replace")
        self.custom_strings = strings
        self.global_subrs = global_subrs
        self.top = _parse_dict(top_dicts[0])

        charstrings_offset, = _int_operand(self.top.get((17,)), "CFF CharStrings")
        self.charstrings, _ = _read_index(data, charstrings_offset, "CFF CharStrings INDEX")
        if not self.charstrings:
            raise CFFError("CFF CharStrings INDEX is empty")
        if len(self.charstrings) > MAX_GLYPHS:
            raise CFFError("CFF glyph count exceeds owned limit")

        charset_offset = 0
        if (15,) in self.top:
            charset_offset, = _int_operand(self.top[(15,)], "CFF charset")
        self.charset = _read_charset(data, charset_offset, len(self.charstrings))

        matrix_values = self.top.get((12, 7))
        if matrix_values is None:
            self.font_matrix = (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
        else:
            if len(matrix_values) != 6:
                raise CFFError("CFF FontMatrix requires six operands")
            self.font_matrix = tuple(float(value) for value in matrix_values)
            a, b, c, d, _, _ = self.font_matrix
            if abs(a * d - b * c) < 1e-18:
                raise CFFError("CFF FontMatrix is singular")

        self.cid_keyed = (12, 30) in self.top
        self.private = _private_data(data, self.top.get((18,)))
        self.fd_array: tuple[_FDData, ...] = ()
        self.fd_select: tuple[int, ...] | None = None

        if self.cid_keyed:
            if (12, 36) not in self.top or (12, 37) not in self.top:
                raise CFFError("CID-keyed CFF requires FDArray and FDSelect")
            fd_array_offset, = _int_operand(self.top[(12, 36)], "CFF FDArray")
            fd_dicts, _ = _read_index(data, fd_array_offset, "CFF FDArray INDEX")
            if not fd_dicts:
                raise CFFError("CID-keyed CFF FDArray is empty")
            parsed_fds = []
            for raw in fd_dicts:
                dictionary = _parse_dict(raw)
                parsed_fds.append(_FDData(_private_data(data, dictionary.get((18,)))))
            self.fd_array = tuple(parsed_fds)
            fd_select_offset, = _int_operand(self.top[(12, 37)], "CFF FDSelect")
            self.fd_select = _read_fdselect(
                data,
                fd_select_offset,
                len(self.charstrings),
                len(self.fd_array),
            )

        self._name_to_gid: dict[str, int] | None = None
        self._cid_to_gid: dict[int, int] | None = None

    @property
    def glyph_count(self) -> int:
        return len(self.charstrings)

    def _private_for_gid(self, gid: int) -> _PrivateData:
        if not 0 <= gid < self.glyph_count:
            raise CFFError(f"CFF glyph id {gid} is out of range")
        if self.fd_select is None:
            return self.private
        return self.fd_array[self.fd_select[gid]].private

    def outline(self, gid: int) -> CFFOutline:
        if not 0 <= gid < self.glyph_count:
            raise CFFError(f"CFF glyph id {gid} is out of range")
        return _Type2Interpreter(
            self.charstrings[gid],
            global_subrs=self.global_subrs,
            private=self._private_for_gid(gid),
        ).run()

    def glyph_id_for_name(self, name: str) -> int | None:
        if self.cid_keyed:
            raise CFFError("glyph-name lookup is not valid for CID-keyed CFF")
        if self._name_to_gid is None:
            mapping: dict[str, int] = {}
            for gid, sid in enumerate(self.charset):
                mapping.setdefault(_sid_name(sid, self.custom_strings), gid)
            self._name_to_gid = mapping
        return self._name_to_gid.get(name)

    def glyph_id_for_cid(self, cid: int) -> int | None:
        if not self.cid_keyed:
            raise CFFError("CID lookup is valid only for CID-keyed CFF")
        if self._cid_to_gid is None:
            self._cid_to_gid = {
                cid_value: gid for gid, cid_value in enumerate(self.charset)
            }
        return self._cid_to_gid.get(cid)
