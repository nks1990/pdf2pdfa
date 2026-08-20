"""Strict owned Adobe Type 1 PFA/PFB font core.

It parses PFA/PFB envelopes, decrypts eexec and Type 1 CharStrings, extracts
Private/Subrs/CharStrings and interprets ordinary Type 1 outline operators.
No Type1-to-TrueType/CFF conversion and no external font engine is used.

Composite ``seac`` and PostScript ``callothersubr``/Flex remain explicit
fail-closed paths until their full semantics are owned and regression-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


class Type1Error(ValueError):
    pass


class UnsupportedType1Error(Type1Error):
    pass


_C1 = 52845
_C2 = 22719
_EEXEC_SEED = 55665
_CHARSTRING_SEED = 4330
_MAX_PROGRAM_BYTES = 64 * 1024 * 1024
_MAX_GLYPHS = 1_000_000
_MAX_SUBRS = 1_000_000
_MAX_OPS = 1_000_000
_MAX_RECURSION = 64
_MAX_STACK = 96


@dataclass(frozen=True, slots=True)
class Type1Command:
    operator: str
    values: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class Type1Outline:
    commands: tuple[Type1Command, ...]
    width_x: float | None
    width_y: float = 0.0


@dataclass(slots=True)
class _State:
    x: float = 0.0
    y: float = 0.0
    start_x: float = 0.0
    start_y: float = 0.0
    have_subpath: bool = False
    width_x: float | None = None
    width_y: float = 0.0
    operations: int = 0


def _decrypt(data: bytes, seed: int) -> bytes:
    r = seed & 0xFFFF
    out = bytearray(len(data))
    for i, cipher in enumerate(data):
        plain = cipher ^ (r >> 8)
        out[i] = plain
        r = ((cipher + r) * _C1 + _C2) & 0xFFFF
    return bytes(out)


def _encrypt_for_tests(data: bytes, seed: int) -> bytes:
    r = seed & 0xFFFF
    out = bytearray(len(data))
    for i, plain in enumerate(data):
        cipher = plain ^ (r >> 8)
        out[i] = cipher
        r = ((cipher + r) * _C1 + _C2) & 0xFFFF
    return bytes(out)


def _pfb_segments(data: bytes) -> list[tuple[int, bytes]] | None:
    if not data.startswith(b"\x80"):
        return None
    result: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data) or data[pos] != 0x80:
            raise Type1Error("malformed PFB segment header")
        kind = data[pos + 1]
        pos += 2
        if kind == 3:
            if pos != len(data):
                trailer = data[pos:]
                if any(byte not in b"\x00\x09\x0a\x0c\x0d\x20" for byte in trailer):
                    raise Type1Error("non-whitespace data follows PFB end segment")
            return result
        if kind not in (1, 2):
            raise Type1Error(f"unsupported PFB segment type {kind}")
        if pos + 4 > len(data):
            raise Type1Error("truncated PFB segment length")
        length = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4
        if pos + length > len(data):
            raise Type1Error("PFB segment length exceeds input")
        result.append((kind, data[pos : pos + length]))
        pos += length
    raise Type1Error("PFB program has no end segment")


def _ascii_hex_payload(data: bytes) -> bytes | None:
    probe = [byte for byte in data[:256] if byte not in b"\x00\x09\x0a\x0c\x0d\x20"]
    if len(probe) < 16 or not all(chr(byte) in "0123456789abcdefABCDEF" for byte in probe[:16]):
        return None
    digits = bytearray()
    for byte in data:
        if byte in b"\x00\x09\x0a\x0c\x0d\x20":
            continue
        if chr(byte) not in "0123456789abcdefABCDEF":
            break
        digits.append(byte)
    if len(digits) < 8:
        raise Type1Error("PFA eexec section contains too few hexadecimal digits")
    if len(digits) & 1:
        digits.pop()
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError as exc:
        raise Type1Error("invalid hexadecimal eexec section") from exc


def _extract_eexec(data: bytes) -> tuple[bytes, bytes]:
    if len(data) > _MAX_PROGRAM_BYTES:
        raise Type1Error("Type1 font program exceeds owned size limit")
    segments = _pfb_segments(data)
    if segments is not None:
        clear: list[bytes] = []
        encrypted: list[bytes] = []
        binary_started = False
        for kind, payload in segments:
            if kind == 2:
                encrypted.append(payload)
                binary_started = True
            elif not binary_started:
                clear.append(payload)
        if not encrypted:
            raise Type1Error("PFB Type1 program has no binary eexec segment")
        return b"".join(clear), b"".join(encrypted)

    marker = re.search(rb"\beexec\b", data)
    if marker is None:
        raise Type1Error("Type1 PFA program has no eexec section")
    clear = data[: marker.end()]
    remainder = data[marker.end() :].lstrip(b"\x00\x09\x0a\x0c\x0d\x20")
    hexadecimal = _ascii_hex_payload(remainder)
    return clear, hexadecimal if hexadecimal is not None else remainder


def _parse_matrix(clear: bytes) -> tuple[float, float, float, float, float, float]:
    match = re.search(rb"/FontMatrix\s*\[([^\]]+)\]", clear, re.S)
    if match is None:
        return (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
    tokens = match.group(1).split()
    if len(tokens) != 6:
        raise Type1Error("Type1 FontMatrix shall contain six numbers")
    try:
        values = tuple(float(token) for token in tokens)
    except ValueError as exc:
        raise Type1Error("Type1 FontMatrix contains a non-number") from exc
    if any(not math.isfinite(value) for value in values):
        raise Type1Error("Type1 FontMatrix contains a non-finite number")
    if abs(values[0] * values[3] - values[1] * values[2]) <= 1e-18:
        raise Type1Error("Type1 FontMatrix is singular")
    return values  # type: ignore[return-value]


def _parse_font_name(clear: bytes) -> str:
    match = re.search(rb"/FontName\s+/([^\s<>{}\[\]()/]+)", clear)
    return match.group(1).decode("latin-1") if match else ""


def _len_iv(private: bytes) -> int:
    match = re.search(rb"/lenIV\s+(-?\d+)", private)
    value = 4 if match is None else int(match.group(1))
    if value < -1 or value > 64:
        raise Type1Error(f"unsupported Type1 lenIV {value}")
    return value


def _decode_charstring(data: bytes, len_iv: int) -> bytes:
    if len_iv == -1:
        return data
    decoded = _decrypt(data, _CHARSTRING_SEED)
    if len(decoded) < len_iv:
        raise Type1Error("Type1 CharString shorter than lenIV")
    return decoded[len_iv:]


def _extract_subrs(private: bytes, len_iv: int) -> list[bytes | None]:
    header = re.search(rb"/Subrs\s+(\d+)\s+array", private)
    if header is None:
        return []
    count = int(header.group(1))
    if count > _MAX_SUBRS:
        raise Type1Error("Type1 Subrs count exceeds owned limit")
    result: list[bytes | None] = [None] * count
    pattern = re.compile(rb"\bdup\s+(\d+)\s+(\d+)\s+(?:RD|-\|)\s")
    cursor = header.end()
    seen: set[int] = set()
    while len(seen) < count:
        match = pattern.search(private, cursor)
        if match is None:
            break
        index = int(match.group(1))
        length = int(match.group(2))
        start, end = match.end(), match.end() + length
        if end > len(private):
            raise Type1Error("truncated Type1 Subr payload")
        if not 0 <= index < count:
            raise Type1Error(f"Type1 Subr index {index} outside declared array")
        if index in seen:
            raise Type1Error(f"duplicate Type1 Subr index {index}")
        result[index] = _decode_charstring(private[start:end], len_iv)
        seen.add(index)
        cursor = end
    return result


def _extract_charstrings(private: bytes, len_iv: int) -> dict[str, bytes]:
    header = re.search(rb"/CharStrings\s+(\d+)\s+dict", private)
    if header is None:
        raise Type1Error("Type1 Private dictionary has no CharStrings dictionary")
    count = int(header.group(1))
    if count <= 0 or count > _MAX_GLYPHS:
        raise Type1Error("Type1 CharStrings count is invalid/unsafe")
    pattern = re.compile(rb"/([^\s<>{}\[\]()/]+)\s+(\d+)\s+(?:RD|-\|)\s")
    cursor = header.end()
    result: dict[str, bytes] = {}
    while len(result) < count:
        match = pattern.search(private, cursor)
        if match is None:
            break
        name = match.group(1).decode("latin-1")
        length = int(match.group(2))
        start, end = match.end(), match.end() + length
        if end > len(private):
            raise Type1Error(f"truncated Type1 CharString /{name}")
        if name in result:
            raise Type1Error(f"duplicate Type1 CharString /{name}")
        result[name] = _decode_charstring(private[start:end], len_iv)
        cursor = end
    if len(result) != count:
        raise Type1Error(
            f"Type1 CharStrings dictionary declared {count} glyphs but {len(result)} were readable"
        )
    if ".notdef" not in result:
        raise Type1Error("Type1 font has no /.notdef CharString")
    return result


def _read_number(data: bytes, position: int) -> tuple[float, int, bool] | None:
    if position >= len(data):
        return None
    byte = data[position]
    if 32 <= byte <= 246:
        return float(byte - 139), position + 1, False
    if 247 <= byte <= 250:
        if position + 1 >= len(data):
            raise Type1Error("truncated Type1 positive number")
        value = (byte - 247) * 256 + data[position + 1] + 108
        return float(value), position + 2, False
    if 251 <= byte <= 254:
        if position + 1 >= len(data):
            raise Type1Error("truncated Type1 negative number")
        value = -(byte - 251) * 256 - data[position + 1] - 108
        return float(value), position + 2, False
    if byte == 255:
        if position + 4 >= len(data):
            raise Type1Error("truncated Type1 32-bit number")
        value = int.from_bytes(data[position + 1 : position + 5], "big", signed=True)
        return float(value), position + 5, abs(value) > 32000
    return None


def _exact_int(value: float, label: str) -> int:
    integer = int(value)
    if integer != value:
        raise Type1Error(f"{label} shall be an integer")
    return integer


def _take(stack: list[float], count: int, label: str) -> list[float]:
    if len(stack) != count:
        raise Type1Error(f"Type1 {label} expects {count} operand(s), got {len(stack)}")
    values = stack[:]
    stack.clear()
    return values


def _move(commands: list[Type1Command], state: _State, x: float, y: float) -> None:
    state.x, state.y = x, y
    state.start_x, state.start_y = x, y
    state.have_subpath = True
    commands.append(Type1Command("M", (x, y)))


def _line(commands: list[Type1Command], state: _State, x: float, y: float) -> None:
    state.x, state.y = x, y
    commands.append(Type1Command("L", (x, y)))


def _curve(commands, state, c1, c2, end) -> None:
    state.x, state.y = end
    commands.append(Type1Command("C", (*c1, *c2, *end)))


def _close(commands: list[Type1Command], state: _State) -> None:
    if state.have_subpath:
        commands.append(Type1Command("Z"))
        state.x, state.y = state.start_x, state.start_y
        state.have_subpath = False


class _Interpreter:
    def __init__(self, subrs: list[bytes | None]) -> None:
        self.subrs = subrs
        self.commands: list[Type1Command] = []
        self.state = _State()
        self.stack: list[float] = []

    def run(self, data: bytes) -> Type1Outline:
        signal = self._execute(data, depth=0, is_subr=False)
        if signal != "end":
            raise Type1Error("Type1 top-level CharString did not terminate with endchar")
        if self.stack:
            raise Type1Error("Type1 CharString ended with operands on stack")
        return Type1Outline(tuple(self.commands), self.state.width_x, self.state.width_y)

    def _tick(self) -> None:
        self.state.operations += 1
        if self.state.operations > _MAX_OPS:
            raise Type1Error("Type1 CharString exceeds owned operation limit")

    def _execute(self, data: bytes, *, depth: int, is_subr: bool) -> str:
        if depth > _MAX_RECURSION:
            raise Type1Error("Type1 Subr recursion exceeds owned limit")
        pos = 0
        large_div_stage = 0
        while pos < len(data):
            self._tick()
            number = _read_number(data, pos)
            if number is not None:
                value, pos, large = number
                if large_div_stage == 2:
                    raise Type1Error("Type1 large integer must be followed by one denominator and div")
                if large_div_stage == 1:
                    if large:
                        raise Type1Error("Type1 large-integer div denominator shall be a regular integer")
                    large_div_stage = 2
                elif large:
                    large_div_stage = 1
                self.stack.append(value)
                if len(self.stack) > _MAX_STACK:
                    raise Type1Error("Type1 operand stack exceeds owned limit")
                continue

            op = data[pos]
            pos += 1
            if op == 12:
                if pos >= len(data):
                    raise Type1Error("truncated Type1 escape operator")
                escape = data[pos]
                pos += 1
                if large_div_stage:
                    if not (large_div_stage == 2 and escape == 12):
                        raise Type1Error("Type1 large integer is only valid immediately before div")
                    large_div_stage = 0
                self._escape(escape)
                continue
            if large_div_stage:
                raise Type1Error("Type1 large integer is only valid immediately before div")

            if op == 1:
                _take(self.stack, 2, "hstem")
            elif op == 3:
                _take(self.stack, 2, "vstem")
            elif op == 4:
                (dy,) = _take(self.stack, 1, "vmoveto")
                _move(self.commands, self.state, self.state.x, self.state.y + dy)
            elif op == 5:
                dx, dy = _take(self.stack, 2, "rlineto")
                _line(self.commands, self.state, self.state.x + dx, self.state.y + dy)
            elif op == 6:
                (dx,) = _take(self.stack, 1, "hlineto")
                _line(self.commands, self.state, self.state.x + dx, self.state.y)
            elif op == 7:
                (dy,) = _take(self.stack, 1, "vlineto")
                _line(self.commands, self.state, self.state.x, self.state.y + dy)
            elif op == 8:
                dx1, dy1, dx2, dy2, dx3, dy3 = _take(self.stack, 6, "rrcurveto")
                c1 = (self.state.x + dx1, self.state.y + dy1)
                c2 = (c1[0] + dx2, c1[1] + dy2)
                end = (c2[0] + dx3, c2[1] + dy3)
                _curve(self.commands, self.state, c1, c2, end)
            elif op == 9:
                _take(self.stack, 0, "closepath")
                _close(self.commands, self.state)
            elif op == 10:
                if not self.stack:
                    raise Type1Error("Type1 callsubr requires a Subr index")
                index = _exact_int(self.stack.pop(), "Type1 Subr index")
                if not 0 <= index < len(self.subrs) or self.subrs[index] is None:
                    raise Type1Error(f"Type1 callsubr references missing Subr {index}")
                signal = self._execute(self.subrs[index] or b"", depth=depth + 1, is_subr=True)
                if signal != "return":
                    raise Type1Error("Type1 Subr terminated without return")
            elif op == 11:
                if not is_subr:
                    raise Type1Error("Type1 return outside Subr")
                return "return"
            elif op == 13:
                sbx, wx = _take(self.stack, 2, "hsbw")
                self.state.x, self.state.y = sbx, 0.0
                self.state.width_x, self.state.width_y = wx, 0.0
            elif op == 14:
                _take(self.stack, 0, "endchar")
                if is_subr:
                    raise Type1Error("Type1 endchar encountered inside Subr")
                return "end"
            elif op == 21:
                dx, dy = _take(self.stack, 2, "rmoveto")
                _move(self.commands, self.state, self.state.x + dx, self.state.y + dy)
            elif op == 22:
                (dx,) = _take(self.stack, 1, "hmoveto")
                _move(self.commands, self.state, self.state.x + dx, self.state.y)
            elif op == 30:
                dy1, dx2, dy2, dx3 = _take(self.stack, 4, "vhcurveto")
                c1 = (self.state.x, self.state.y + dy1)
                c2 = (c1[0] + dx2, c1[1] + dy2)
                _curve(self.commands, self.state, c1, c2, (c2[0] + dx3, c2[1]))
            elif op == 31:
                dx1, dx2, dy2, dy3 = _take(self.stack, 4, "hvcurveto")
                c1 = (self.state.x + dx1, self.state.y)
                c2 = (c1[0] + dx2, c1[1] + dy2)
                _curve(self.commands, self.state, c1, c2, (c2[0], c2[1] + dy3))
            else:
                raise UnsupportedType1Error(f"unsupported Type1 CharString operator {op}")
        if large_div_stage:
            raise Type1Error("Type1 large integer is not completed by div")
        if is_subr:
            raise Type1Error("Type1 Subr reached end without return")
        raise Type1Error("Type1 CharString reached end without endchar")

    def _escape(self, op: int) -> None:
        if op == 0:
            _take(self.stack, 0, "dotsection")
        elif op in (1, 2):
            _take(self.stack, 6, "vstem3/hstem3")
        elif op == 6:
            _take(self.stack, 5, "seac")
            raise UnsupportedType1Error(
                "Type1 seac composite requires owned StandardEncoding composition"
            )
        elif op == 7:
            sbx, sby, wx, wy = _take(self.stack, 4, "sbw")
            self.state.x, self.state.y = sbx, sby
            self.state.width_x, self.state.width_y = wx, wy
        elif op == 12:
            if len(self.stack) < 2:
                raise Type1Error("Type1 div requires two operands")
            denominator = self.stack.pop()
            numerator = self.stack.pop()
            if denominator == 0:
                raise Type1Error("Type1 div by zero")
            self.stack.append(numerator / denominator)
        elif op == 16:
            raise UnsupportedType1Error(
                "Type1 callothersubr/Flex requires owned OtherSubrs semantics"
            )
        elif op == 17:
            raise UnsupportedType1Error(
                "Type1 pop is only supported with owned OtherSubrs semantics"
            )
        elif op == 33:
            x, y = _take(self.stack, 2, "setcurrentpoint")
            self.state.x, self.state.y = x, y
        else:
            raise UnsupportedType1Error(f"unsupported Type1 escaped operator 12 {op}")


class Type1Font:
    """Parsed strict Type 1 font program."""

    def __init__(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Type1 font data must be bytes")
        clear, ciphertext = _extract_eexec(bytes(data))
        decrypted = _decrypt(ciphertext, _EEXEC_SEED)
        if len(decrypted) < 4:
            raise Type1Error("Type1 eexec section is shorter than random prefix")
        private = decrypted[4:]
        self.font_name = _parse_font_name(clear)
        self.font_matrix = _parse_matrix(clear)
        self.len_iv = _len_iv(private)
        self.subrs = _extract_subrs(private, self.len_iv)
        self.charstrings = _extract_charstrings(private, self.len_iv)

    @property
    def glyph_names(self) -> tuple[str, ...]:
        return tuple(self.charstrings)

    def has_glyph(self, name: str) -> bool:
        return name in self.charstrings

    def outline(self, name: str) -> Type1Outline:
        data = self.charstrings.get(name) or self.charstrings.get(".notdef")
        if data is None:
            raise Type1Error("Type1 font has no /.notdef fallback")
        return _Interpreter(self.subrs).run(data)
