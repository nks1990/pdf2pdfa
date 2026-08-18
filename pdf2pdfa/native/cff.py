"""Owned CFF1 parser and Type 2 CharString outline interpreter.

This module reads embedded CFF programs directly; it never converts them to
TrueType or delegates font execution.  It covers the CFF1 structures needed by
PDF Type1C/CIDFontType0C programs: INDEX/DICT, ISOAdobe/custom charset,
Private/Subrs, CID FDArray/FDSelect, FontMatrix metadata and Type 2 drawing,
hints, flex, subroutines and deterministic stack arithmetic.

Unsupported format features fail closed rather than being guessed.  In
particular CFF2, predefined Expert charsets, Type2 ``random`` and deprecated
``endchar`` seac composition are deliberately explicit gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


class CFFError(ValueError):
    pass


class UnsupportedCFFError(CFFError):
    pass


MAX_INDEX_COUNT = 1_000_000
MAX_GLYPHS = 1_000_000
MAX_STACK = 48
MAX_SUBR_DEPTH = 32
MAX_OPERATIONS = 1_000_000


_STANDARD_STRINGS = tuple(
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
    subrs: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class _FDData:
    private: _PrivateData
    font_matrix: tuple[float, ...] | None


class _Reader:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    def _take(self, count: int, label: str) -> bytes:
        if count < 0 or self.position + count > len(self.data):
            raise CFFError(f"truncated CFF while reading {label}")
        result = self.data[self.position : self.position + count]
        self.position += count
        return result

    def u8(self, label: str) -> int:
        return self._take(1, label)[0]

    def u16(self, label: str) -> int:
        return int.from_bytes(self._take(2, label), "big")


def _offset(reader: _Reader, size: int, label: str) -> int:
    if size not in (1, 2, 3, 4):
        raise CFFError(f"invalid CFF offSize {size}")
    return int.from_bytes(reader._take(size, label), "big")


def _index(data: bytes, start: int, label: str) -> tuple[tuple[bytes, ...], int]:
    reader = _Reader(data, start)
    count = reader.u16(f"{label} count")
    if count > MAX_INDEX_COUNT:
        raise CFFError(f"{label} count exceeds owned limit")
    if count == 0:
        return (), reader.position
    off_size = reader.u8(f"{label} offSize")
    offsets = [_offset(reader, off_size, f"{label} offset") for _ in range(count + 1)]
    if offsets[0] != 1:
        raise CFFError(f"{label} first offset shall be 1")
    if any(item <= 0 for item in offsets):
        raise CFFError(f"{label} contains a non-positive offset")
    if any(right < left for left, right in zip(offsets, offsets[1:])):
        raise CFFError(f"{label} offsets are not monotonic")
    body = reader.position
    end = body + offsets[-1] - 1
    if end > len(data):
        raise CFFError(f"{label} body extends past the font")
    values = tuple(
        data[body + offsets[i] - 1 : body + offsets[i + 1] - 1]
        for i in range(count)
    )
    return values, end


def _dict_number(data: bytes, pos: int) -> tuple[float, int]:
    if pos >= len(data):
        raise CFFError("truncated CFF DICT number")
    b0 = data[pos]
    pos += 1
    if 32 <= b0 <= 246:
        return float(b0 - 139), pos
    if 247 <= b0 <= 250:
        if pos >= len(data):
            raise CFFError("truncated positive CFF DICT number")
        return float((b0 - 247) * 256 + data[pos] + 108), pos + 1
    if 251 <= b0 <= 254:
        if pos >= len(data):
            raise CFFError("truncated negative CFF DICT number")
        return float(-(b0 - 251) * 256 - data[pos] - 108), pos + 1
    if b0 == 28:
        if pos + 2 > len(data):
            raise CFFError("truncated CFF DICT shortint")
        return float(int.from_bytes(data[pos : pos + 2], "big", signed=True)), pos + 2
    if b0 == 29:
        if pos + 4 > len(data):
            raise CFFError("truncated CFF DICT longint")
        return float(int.from_bytes(data[pos : pos + 4], "big", signed=True)), pos + 4
    if b0 == 30:
        text: list[str] = []
        done = False
        while not done:
            if pos >= len(data):
                raise CFFError("truncated CFF DICT real")
            byte = data[pos]
            pos += 1
            for nibble in (byte >> 4, byte & 15):
                if nibble <= 9:
                    text.append(str(nibble))
                elif nibble == 10:
                    text.append(".")
                elif nibble == 11:
                    text.append("E")
                elif nibble == 12:
                    text.append("E-")
                elif nibble == 14:
                    text.append("-")
                elif nibble == 15:
                    done = True
                    break
                else:
                    raise CFFError("reserved nibble in CFF DICT real")
        try:
            value = float("".join(text))
        except ValueError as exc:
            raise CFFError("invalid CFF DICT real") from exc
        if not math.isfinite(value):
            raise CFFError("non-finite CFF DICT real")
        return value, pos
    raise CFFError(f"byte {b0} is not a CFF DICT number")


def _dictionary(data: bytes) -> dict[tuple[int, ...], tuple[float, ...]]:
    result: dict[tuple[int, ...], tuple[float, ...]] = {}
    stack: list[float] = []
    pos = 0
    while pos < len(data):
        b0 = data[pos]
        if b0 in (28, 29, 30) or b0 >= 32:
            value, pos = _dict_number(data, pos)
            stack.append(value)
            if len(stack) > MAX_STACK:
                raise CFFError("CFF DICT stack exceeds 48 operands")
            continue
        pos += 1
        if b0 == 12:
            if pos >= len(data):
                raise CFFError("truncated escaped CFF DICT operator")
            operator = (12, data[pos])
            pos += 1
        elif 0 <= b0 <= 21:
            operator = (b0,)
        else:
            raise CFFError(f"reserved CFF DICT operator {b0}")
        if operator in result:
            raise CFFError(f"duplicate CFF DICT operator {operator}")
        result[operator] = tuple(stack)
        stack.clear()
    if stack:
        raise CFFError("CFF DICT ends with unused operands")
    return result


def _integers(values: tuple[float, ...] | None, label: str, count: int) -> tuple[int, ...]:
    if values is None or len(values) != count:
        raise CFFError(f"{label} requires {count} integer operand(s)")
    result: list[int] = []
    for value in values:
        integer = int(value)
        if integer != value:
            raise CFFError(f"{label} contains a non-integer operand")
        result.append(integer)
    return tuple(result)


def _matrix(values: tuple[float, ...] | None, label: str) -> tuple[float, ...] | None:
    if values is None:
        return None
    if len(values) != 6:
        raise CFFError(f"{label} requires six operands")
    result = tuple(float(item) for item in values)
    a, b, c, d, _, _ = result
    if abs(a * d - b * c) < 1e-18:
        raise CFFError(f"{label} is singular")
    return result


def _bias(count: int) -> int:
    return 107 if count < 1240 else 1131 if count < 33900 else 32768


def _charset(data: bytes, offset: int, glyphs: int, *, cid: bool) -> tuple[int, ...]:
    if not 0 < glyphs <= MAX_GLYPHS:
        raise CFFError(f"invalid CFF glyph count {glyphs}")
    if offset == 0:
        if cid:
            raise CFFError("CID-keyed CFF requires an explicit charset")
        if glyphs > len(_STANDARD_STRINGS):
            raise UnsupportedCFFError("ISOAdobe charset exceeds owned SID table")
        return tuple(range(glyphs))
    if offset in (1, 2):
        raise UnsupportedCFFError("Expert/ExpertSubset predefined CFF charset is not owned yet")
    reader = _Reader(data, offset)
    fmt = reader.u8("CFF charset format")
    output = [0]
    if fmt == 0:
        while len(output) < glyphs:
            output.append(reader.u16("CFF charset SID/CID"))
    elif fmt in (1, 2):
        while len(output) < glyphs:
            first = reader.u16("CFF charset range first")
            n_left = reader.u8("CFF charset nLeft") if fmt == 1 else reader.u16("CFF charset nLeft")
            if len(output) + n_left + 1 > glyphs:
                raise CFFError("CFF charset range exceeds glyph count")
            output.extend(first + i for i in range(n_left + 1))
    else:
        raise CFFError(f"unsupported CFF charset format {fmt}")
    return tuple(output)


def _fdselect(data: bytes, offset: int, glyphs: int, fd_count: int) -> tuple[int, ...]:
    reader = _Reader(data, offset)
    fmt = reader.u8("CFF FDSelect format")
    if fmt == 0:
        result = tuple(reader.u8("CFF FDSelect entry") for _ in range(glyphs))
    elif fmt == 3:
        count = reader.u16("CFF FDSelect range count")
        ranges: list[tuple[int, int]] = []
        previous = -1
        for _ in range(count):
            first = reader.u16("CFF FDSelect first")
            fd = reader.u8("CFF FDSelect fd")
            if first <= previous:
                raise CFFError("CFF FDSelect ranges are not increasing")
            previous = first
            ranges.append((first, fd))
        sentinel = reader.u16("CFF FDSelect sentinel")
        if not ranges or ranges[0][0] != 0 or sentinel != glyphs:
            raise CFFError("invalid CFF FDSelect format 3 boundaries")
        values = [0] * glyphs
        for i, (first, fd) in enumerate(ranges):
            stop = ranges[i + 1][0] if i + 1 < len(ranges) else sentinel
            if stop > glyphs:
                raise CFFError("CFF FDSelect range exceeds glyph count")
            values[first:stop] = [fd] * (stop - first)
        result = tuple(values)
    else:
        raise UnsupportedCFFError(f"CFF1 FDSelect format {fmt} is not implemented")
    if any(fd >= fd_count for fd in result):
        raise CFFError("CFF FDSelect references a missing Font DICT")
    return result


def _private(data: bytes, operands: tuple[float, ...] | None) -> _PrivateData:
    if operands is None:
        return _PrivateData(0.0, 0.0, ())
    size, offset = _integers(operands, "CFF Private", 2)
    if size < 0 or offset < 0 or offset + size > len(data):
        raise CFFError("CFF Private DICT lies outside font data")
    dictionary = _dictionary(data[offset : offset + size])
    default = dictionary.get((20,), (0.0,))
    nominal = dictionary.get((21,), (0.0,))
    if len(default) != 1 or len(nominal) != 1:
        raise CFFError("CFF private width operators require one operand")
    subrs: tuple[bytes, ...] = ()
    if (19,) in dictionary:
        relative, = _integers(dictionary[(19,)], "CFF Subrs", 1)
        subrs, _ = _index(data, offset + relative, "CFF Local Subr INDEX")
    return _PrivateData(default[0], nominal[0], subrs)


def _sid_name(sid: int, custom: tuple[bytes, ...]) -> str:
    if 0 <= sid < len(_STANDARD_STRINGS):
        return _STANDARD_STRINGS[sid]
    if sid < 391:
        raise UnsupportedCFFError(f"CFF standard Expert SID {sid} name is not owned yet")
    index = sid - 391
    if not 0 <= index < len(custom):
        raise CFFError(f"CFF SID {sid} references a missing String INDEX entry")
    try:
        return custom[index].decode("ascii")
    except UnicodeDecodeError as exc:
        raise CFFError(f"CFF custom SID {sid} is not ASCII") from exc


class _Type2:
    def __init__(self, program: bytes, global_subrs: tuple[bytes, ...], private: _PrivateData) -> None:
        self.program = program
        self.global_subrs = global_subrs
        self.private = private
        self.stack: list[float] = []
        self.transient = [0.0] * 32
        self.x = 0.0
        self.y = 0.0
        self.commands: list[CFFCommand] = []
        self.contour = False
        self.stems = 0
        self.width: float | None = None
        self.operations = 0
        self.ended = False

    def _push(self, value: float) -> None:
        if not math.isfinite(value):
            raise CFFError("Type2 produced a non-finite number")
        self.stack.append(float(value))
        if len(self.stack) > MAX_STACK:
            raise CFFError("Type2 stack exceeds 48 operands")

    def _pop(self, label: str) -> float:
        if not self.stack:
            raise CFFError(f"Type2 {label} requires an operand")
        return self.stack.pop()

    def _pop_int(self, label: str) -> int:
        value = self._pop(label)
        integer = int(value)
        if integer != value:
            raise CFFError(f"Type2 {label} requires an integer")
        return integer

    def _close(self) -> None:
        if self.contour:
            self.commands.append(CFFCommand("Z", ()))
            self.contour = False

    def _move(self, dx: float, dy: float) -> None:
        self._close()
        self.x += dx
        self.y += dy
        self.commands.append(CFFCommand("M", (self.x, self.y)))
        self.contour = True

    def _line(self, dx: float, dy: float) -> None:
        if not self.contour:
            raise CFFError("Type2 line before moveto")
        self.x += dx
        self.y += dy
        self.commands.append(CFFCommand("L", (self.x, self.y)))

    def _curve(self, values: Iterable[float]) -> None:
        values = tuple(values)
        if len(values) != 6 or not self.contour:
            raise CFFError("Type2 curve requires six deltas after moveto")
        dx1, dy1, dx2, dy2, dx3, dy3 = values
        x1, y1 = self.x + dx1, self.y + dy1
        x2, y2 = x1 + dx2, y1 + dy2
        self.x, self.y = x2 + dx3, y2 + dy3
        self.commands.append(CFFCommand("C", (x1, y1, x2, y2, self.x, self.y)))

    def _width_stem(self) -> None:
        if self.width is None and len(self.stack) % 2:
            self.width = self.private.nominal_width + self.stack.pop(0)

    def _width_move(self, required: int) -> None:
        if self.width is None and len(self.stack) == required + 1:
            self.width = self.private.nominal_width + self.stack.pop(0)

    def _stem(self) -> None:
        self._width_stem()
        if len(self.stack) % 2:
            raise CFFError("Type2 stem operator requires pairs")
        self.stems += len(self.stack) // 2
        self.stack.clear()

    def _number(self, data: bytes, pos: int) -> tuple[float, int]:
        b0 = data[pos]
        pos += 1
        if 32 <= b0 <= 246:
            return float(b0 - 139), pos
        if 247 <= b0 <= 250:
            if pos >= len(data):
                raise CFFError("truncated positive Type2 number")
            return float((b0 - 247) * 256 + data[pos] + 108), pos + 1
        if 251 <= b0 <= 254:
            if pos >= len(data):
                raise CFFError("truncated negative Type2 number")
            return float(-(b0 - 251) * 256 - data[pos] - 108), pos + 1
        if b0 == 28:
            if pos + 2 > len(data):
                raise CFFError("truncated Type2 shortint")
            return float(int.from_bytes(data[pos : pos + 2], "big", signed=True)), pos + 2
        if b0 == 255:
            if pos + 4 > len(data):
                raise CFFError("truncated Type2 16.16 number")
            raw = int.from_bytes(data[pos : pos + 4], "big", signed=True)
            return raw / 65536.0, pos + 4
        raise CFFError(f"byte {b0} is not a Type2 number")

    def _call(self, global_call: bool, depth: int) -> None:
        raw = self._pop_int("subroutine index")
        subrs = self.global_subrs if global_call else self.private.subrs
        index = raw + _bias(len(subrs))
        if not 0 <= index < len(subrs):
            raise CFFError(f"Type2 subroutine index {index} is out of range")
        if depth >= MAX_SUBR_DEPTH:
            raise CFFError("Type2 subroutine recursion exceeds owned limit")
        if not self._execute(subrs[index], depth + 1, subroutine=True):
            raise CFFError("Type2 subroutine ended without return")

    def _escape(self, op: int) -> None:
        s = self.stack
        if op == 3:
            b, a = self._pop("and"), self._pop("and"); self._push(1.0 if a and b else 0.0)
        elif op == 4:
            b, a = self._pop("or"), self._pop("or"); self._push(1.0 if a or b else 0.0)
        elif op == 5:
            self._push(0.0 if self._pop("not") else 1.0)
        elif op == 9:
            self._push(abs(self._pop("abs")))
        elif op == 10:
            b, a = self._pop("add"), self._pop("add"); self._push(a + b)
        elif op == 11:
            b, a = self._pop("sub"), self._pop("sub"); self._push(a - b)
        elif op == 12:
            b, a = self._pop("div"), self._pop("div")
            if b == 0: raise CFFError("Type2 division by zero")
            self._push(a / b)
        elif op == 14:
            self._push(-self._pop("neg"))
        elif op == 15:
            b, a = self._pop("eq"), self._pop("eq"); self._push(1.0 if a == b else 0.0)
        elif op == 18:
            self._pop("drop")
        elif op == 20:
            index = self._pop_int("put index"); value = self._pop("put value")
            if not 0 <= index < 32: raise CFFError("Type2 put index out of range")
            self.transient[index] = value
        elif op == 21:
            index = self._pop_int("get index")
            if not 0 <= index < 32: raise CFFError("Type2 get index out of range")
            self._push(self.transient[index])
        elif op == 22:
            v2, v1 = self._pop("ifelse"), self._pop("ifelse")
            s2, s1 = self._pop("ifelse"), self._pop("ifelse")
            self._push(s1 if v1 <= v2 else s2)
        elif op == 23:
            raise UnsupportedCFFError("Type2 random is intentionally fail-closed")
        elif op == 24:
            b, a = self._pop("mul"), self._pop("mul"); self._push(a * b)
        elif op == 26:
            value = self._pop("sqrt")
            if value < 0: raise CFFError("Type2 sqrt of negative value")
            self._push(math.sqrt(value))
        elif op == 27:
            value = self._pop("dup"); self._push(value); self._push(value)
        elif op == 28:
            if len(s) < 2: raise CFFError("Type2 exch requires two operands")
            s[-1], s[-2] = s[-2], s[-1]
        elif op == 29:
            index = self._pop_int("index")
            if not s: raise CFFError("Type2 index requires a source operand")
            index = max(0, min(index, len(s) - 1)); self._push(s[-1 - index])
        elif op == 30:
            j = self._pop_int("roll j"); n = self._pop_int("roll n")
            if n < 0 or n > len(s): raise CFFError("Type2 roll n is invalid")
            if n:
                j %= n; tail = s[-n:]; s[-n:] = tail[-j:] + tail[:-j]
        elif op == 34:
            if len(s) != 7: raise CFFError("Type2 hflex requires seven operands")
            dx1, dx2, dy2, dx3, dx4, dx5, dx6 = s
            self._curve((dx1, 0, dx2, dy2, dx3, 0))
            self._curve((dx4, 0, dx5, -dy2, dx6, 0)); s.clear()
        elif op == 35:
            if len(s) != 13: raise CFFError("Type2 flex requires thirteen operands")
            values = list(s); s.clear(); self._curve(values[:6]); self._curve(values[6:12])
        elif op == 36:
            if len(s) != 9: raise CFFError("Type2 hflex1 requires nine operands")
            values = list(s); s.clear()
            dx1, dy1, dx2, dy2, dx3, dx4, dx5, dy5, dx6 = values
            self._curve((dx1, dy1, dx2, dy2, dx3, 0))
            self._curve((dx4, 0, dx5, dy5, dx6, -(dy1 + dy2 + dy5)))
        elif op == 37:
            if len(s) != 11: raise CFFError("Type2 flex1 requires eleven operands")
            values = list(s); s.clear(); first = values[:10]; final = values[10]
            sx = first[0] + first[2] + first[4] + first[6] + first[8]
            sy = first[1] + first[3] + first[5] + first[7] + first[9]
            dx6, dy6 = (final, -sy) if abs(sx) > abs(sy) else (-sx, final)
            self._curve(first[:6]); self._curve((first[6], first[7], first[8], first[9], dx6, dy6))
        else:
            raise UnsupportedCFFError(f"unsupported escaped Type2 operator 12 {op}")

    def _execute(self, data: bytes, depth: int, *, subroutine: bool) -> bool:
        pos = 0
        while pos < len(data):
            self.operations += 1
            if self.operations > MAX_OPERATIONS:
                raise CFFError("Type2 operation count exceeds owned limit")
            b0 = data[pos]
            # Type2 number bytes are 28, 32..254 and 255. Bytes 29/30/31 are
            # callgsubr/vhcurveto/hvcurveto operators and must never enter here.
            if b0 == 28 or 32 <= b0 <= 255:
                value, pos = self._number(data, pos); self._push(value); continue
            pos += 1

            if b0 in (1, 3, 18, 23):
                self._stem()
            elif b0 in (19, 20):
                self._stem(); count = (self.stems + 7) // 8
                if pos + count > len(data): raise CFFError("truncated Type2 hint mask")
                pos += count
            elif b0 == 4:
                self._width_move(1)
                if len(self.stack) != 1: raise CFFError("Type2 vmoveto requires one operand")
                self._move(0, self.stack[0]); self.stack.clear()
            elif b0 == 5:
                if not self.stack or len(self.stack) % 2: raise CFFError("Type2 rlineto requires pairs")
                values = list(self.stack); self.stack.clear()
                for i in range(0, len(values), 2): self._line(values[i], values[i + 1])
            elif b0 in (6, 7):
                if not self.stack: raise CFFError("Type2 h/vlineto requires operands")
                values = list(self.stack); self.stack.clear(); horizontal = b0 == 6
                for value in values:
                    self._line(value if horizontal else 0, 0 if horizontal else value); horizontal = not horizontal
            elif b0 == 8:
                if not self.stack or len(self.stack) % 6: raise CFFError("Type2 rrcurveto requires groups of six")
                values = list(self.stack); self.stack.clear()
                for i in range(0, len(values), 6): self._curve(values[i : i + 6])
            elif b0 == 10:
                self._call(False, depth)
            elif b0 == 11:
                if not subroutine: raise CFFError("Type2 return outside subroutine")
                return True
            elif b0 == 12:
                if pos >= len(data): raise CFFError("truncated escaped Type2 operator")
                op = data[pos]; pos += 1; self._escape(op)
            elif b0 == 14:
                if self.width is None and len(self.stack) in (1, 5):
                    self.width = self.private.nominal_width + self.stack.pop(0)
                if len(self.stack) == 4:
                    raise UnsupportedCFFError("Type2 endchar seac composition is not owned yet")
                if self.stack: raise CFFError("Type2 endchar has unexpected operands")
                self._close(); self.ended = True; return False
            elif b0 == 21:
                self._width_move(2)
                if len(self.stack) != 2: raise CFFError("Type2 rmoveto requires two operands")
                x, y = self.stack; self.stack.clear(); self._move(x, y)
            elif b0 == 22:
                self._width_move(1)
                if len(self.stack) != 1: raise CFFError("Type2 hmoveto requires one operand")
                x = self.stack[0]; self.stack.clear(); self._move(x, 0)
            elif b0 == 24:
                if len(self.stack) < 8 or (len(self.stack) - 2) % 6: raise CFFError("Type2 rcurveline operands invalid")
                values = list(self.stack); self.stack.clear(); stop = len(values) - 2
                for i in range(0, stop, 6): self._curve(values[i : i + 6])
                self._line(values[-2], values[-1])
            elif b0 == 25:
                if len(self.stack) < 8 or (len(self.stack) - 6) % 2: raise CFFError("Type2 rlinecurve operands invalid")
                values = list(self.stack); self.stack.clear(); stop = len(values) - 6
                for i in range(0, stop, 2): self._line(values[i], values[i + 1])
                self._curve(values[-6:])
            elif b0 in (26, 27):
                values = list(self.stack); self.stack.clear(); horizontal = b0 == 27
                extra = values.pop(0) if len(values) % 4 == 1 else 0.0
                if not values or len(values) % 4: raise CFFError("Type2 hh/vvcurveto operands invalid")
                for i in range(0, len(values), 4):
                    a, b, c, d = values[i : i + 4]
                    first = i == 0
                    self._curve((a, extra if first else 0, b, c, d, 0) if horizontal else (extra if first else 0, a, b, c, 0, d))
            elif b0 == 29:
                self._call(True, depth)
            elif b0 in (30, 31):
                values = list(self.stack); self.stack.clear(); horizontal = b0 == 31; i = 0
                if len(values) < 4 or len(values) % 4 not in (0, 1): raise CFFError("Type2 hv/vhcurveto operands invalid")
                while i < len(values):
                    remaining = len(values) - i
                    if remaining == 5:
                        a, b, c, d, extra = values[i : i + 5]; i += 5
                        self._curve((a, 0, b, c, extra, d) if horizontal else (0, a, b, c, d, extra))
                    else:
                        if remaining < 4: raise CFFError("Type2 hv/vhcurveto trailing operands invalid")
                        a, b, c, d = values[i : i + 4]; i += 4
                        self._curve((a, 0, b, c, 0, d) if horizontal else (0, a, b, c, d, 0))
                    horizontal = not horizontal
            else:
                raise UnsupportedCFFError(f"unsupported Type2 operator {b0}")

        if subroutine:
            return False
        raise CFFError("Type2 CharString ended without endchar")

    def run(self) -> CFFOutline:
        self._execute(self.program, 0, subroutine=False)
        if not self.ended: raise CFFError("Type2 CharString did not end")
        return CFFOutline(
            self.private.default_width if self.width is None else self.width,
            tuple(self.commands),
        )


class CFFFont:
    """Parse one CFF1 font and expose original-program glyph outlines."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 4: raise CFFError("CFF data is shorter than its header")
        major, minor, header_size, off_size = data[:4]
        if major != 1: raise UnsupportedCFFError(f"only CFF1 is supported, got {major}.{minor}")
        if not 4 <= header_size <= len(data): raise CFFError("invalid CFF header size")
        if off_size not in (1, 2, 3, 4): raise CFFError("invalid CFF header offSize")
        self.data = data

        names, pos = _index(data, header_size, "CFF Name INDEX")
        tops, pos = _index(data, pos, "CFF Top DICT INDEX")
        strings, pos = _index(data, pos, "CFF String INDEX")
        global_subrs, _ = _index(data, pos, "CFF Global Subr INDEX")
        if len(names) != 1 or len(tops) != 1:
            raise UnsupportedCFFError("owned PDF CFF path requires exactly one font in the CFF set")
        self.name = names[0].decode("ascii", "replace")
        self.custom_strings = strings
        self.global_subrs = global_subrs
        self.top = _dictionary(tops[0])

        charstrings_offset, = _integers(self.top.get((17,)), "CFF CharStrings", 1)
        self.charstrings, _ = _index(data, charstrings_offset, "CFF CharStrings INDEX")
        if not self.charstrings or len(self.charstrings) > MAX_GLYPHS:
            raise CFFError("invalid CFF CharStrings glyph count")

        self.cid_keyed = (12, 30) in self.top
        if self.cid_keyed:
            _integers(self.top[(12, 30)], "CFF ROS", 3)
        charset_offset = 0
        if (15,) in self.top:
            charset_offset, = _integers(self.top[(15,)], "CFF charset", 1)
        self.charset = _charset(data, charset_offset, len(self.charstrings), cid=self.cid_keyed)

        self.font_matrix = _matrix(self.top.get((12, 7)), "CFF FontMatrix") or (
            0.001, 0.0, 0.0, 0.001, 0.0, 0.0
        )
        self.private = _private(data, self.top.get((18,)))
        self.fd_array: tuple[_FDData, ...] = ()
        self.fd_select: tuple[int, ...] | None = None

        if self.cid_keyed:
            fd_array_offset, = _integers(self.top.get((12, 36)), "CFF FDArray", 1)
            fd_select_offset, = _integers(self.top.get((12, 37)), "CFF FDSelect", 1)
            raw_fds, _ = _index(data, fd_array_offset, "CFF FDArray INDEX")
            if not raw_fds: raise CFFError("CID-keyed CFF FDArray is empty")
            fds: list[_FDData] = []
            for raw in raw_fds:
                dictionary = _dictionary(raw)
                fds.append(
                    _FDData(
                        private=_private(data, dictionary.get((18,))),
                        font_matrix=_matrix(dictionary.get((12, 7)), "CFF FD FontMatrix"),
                    )
                )
            self.fd_array = tuple(fds)
            self.fd_select = _fdselect(data, fd_select_offset, len(self.charstrings), len(self.fd_array))

        self._name_map: dict[str, int] | None = None
        self._cid_map: dict[int, int] | None = None

    @property
    def glyph_count(self) -> int:
        return len(self.charstrings)

    def private_for_gid(self, gid: int) -> _PrivateData:
        if not 0 <= gid < self.glyph_count: raise CFFError(f"glyph id {gid} out of range")
        if self.fd_select is None: return self.private
        return self.fd_array[self.fd_select[gid]].private

    def fd_font_matrix(self, gid: int) -> tuple[float, ...] | None:
        if not 0 <= gid < self.glyph_count: raise CFFError(f"glyph id {gid} out of range")
        if self.fd_select is None: return None
        return self.fd_array[self.fd_select[gid]].font_matrix

    def outline(self, gid: int) -> CFFOutline:
        if not 0 <= gid < self.glyph_count: raise CFFError(f"glyph id {gid} out of range")
        return _Type2(self.charstrings[gid], self.global_subrs, self.private_for_gid(gid)).run()

    def glyph_id_for_name(self, name: str) -> int | None:
        if self.cid_keyed: raise CFFError("name lookup is invalid for CID-keyed CFF")
        if self._name_map is None:
            mapping: dict[str, int] = {}
            for gid, sid in enumerate(self.charset): mapping.setdefault(_sid_name(sid, self.custom_strings), gid)
            self._name_map = mapping
        return self._name_map.get(name)

    def glyph_id_for_cid(self, cid: int) -> int | None:
        if not self.cid_keyed: raise CFFError("CID lookup requires CID-keyed CFF")
        if self._cid_map is None: self._cid_map = {cid_value: gid for gid, cid_value in enumerate(self.charset)}
        return self._cid_map.get(cid)
