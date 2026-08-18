"""Owned evaluator for PDF FunctionType 0, 2, 3 and 4."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
import re
from typing import Callable, Iterable

from .document import PDFDocument
from .objects import PDFDict, PDFObject, PDFStream
from .structure import decoded_stream_bytes, resolve


class PDFFunctionError(ValueError):
    pass


def _number(doc: PDFDocument, value: PDFObject, label: str) -> float:
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PDFFunctionError(f"{label} contains a non-number")
    return float(value)


def _numbers(doc: PDFDocument, value: PDFObject | None, label: str) -> list[float]:
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise PDFFunctionError(f"{label} is not an array")
    return [_number(doc, item, label) for item in value]


def _ints(doc: PDFDocument, value: PDFObject | None, label: str) -> list[int]:
    values = _numbers(doc, value, label)
    output = []
    for number in values:
        integer = int(number)
        if integer != number:
            raise PDFFunctionError(f"{label} contains non-integer value")
        output.append(integer)
    return output


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _domain_clip(inputs: Iterable[float], domain: list[float]) -> list[float]:
    values = list(inputs)
    if len(domain) != len(values) * 2:
        raise PDFFunctionError(
            f"function expects {len(domain)//2} inputs, got {len(values)}"
        )
    return [
        _clip(values[index], domain[index * 2], domain[index * 2 + 1])
        for index in range(len(values))
    ]


def _range_clip(values: list[float], range_values: list[float] | None) -> list[float]:
    if range_values is None:
        return values
    if len(range_values) != len(values) * 2:
        raise PDFFunctionError("function Range length does not match outputs")
    return [
        _clip(values[index], range_values[index * 2], range_values[index * 2 + 1])
        for index in range(len(values))
    ]


def _interpolate(x: float, xmin: float, xmax: float, ymin: float, ymax: float) -> float:
    if xmax == xmin:
        return ymin
    return ymin + ((x - xmin) * (ymax - ymin) / (xmax - xmin))


class PDFFunction:
    def __init__(self, doc: PDFDocument, value: PDFObject) -> None:
        self.doc = doc
        resolved = resolve(doc, value)
        self.stream = resolved if isinstance(resolved, PDFStream) else None
        self.dictionary = resolved.dictionary if isinstance(resolved, PDFStream) else resolved
        if not isinstance(self.dictionary, PDFDict):
            raise PDFFunctionError("PDF function is not a dictionary or stream")
        function_type = resolve(doc, self.dictionary.get("FunctionType"))
        if isinstance(function_type, bool) or not isinstance(function_type, int):
            raise PDFFunctionError("FunctionType is missing/invalid")
        self.function_type = function_type
        self.domain = _numbers(doc, self.dictionary.get("Domain"), "Function/Domain")
        if len(self.domain) % 2 or not self.domain:
            raise PDFFunctionError("Function Domain is invalid")
        range_value = self.dictionary.get("Range")
        self.range = _numbers(doc, range_value, "Function/Range") if range_value is not None else None
        if self.range is not None and (len(self.range) % 2 or not self.range):
            raise PDFFunctionError("Function Range is invalid")
        self._evaluator = self._build()

    def _build(self):
        if self.function_type == 0:
            return self._sampled()
        if self.function_type == 2:
            return self._exponential()
        if self.function_type == 3:
            return self._stitching()
        if self.function_type == 4:
            return self._calculator()
        raise PDFFunctionError(f"unsupported FunctionType {self.function_type}")

    def evaluate(self, inputs: Iterable[float]) -> list[float]:
        clipped = _domain_clip(inputs, self.domain)
        values = self._evaluator(clipped)
        return _range_clip(list(values), self.range)

    def _exponential(self):
        c0 = _numbers(self.doc, self.dictionary.get("C0"), "Function/C0") if self.dictionary.get("C0") is not None else [0.0]
        c1 = _numbers(self.doc, self.dictionary.get("C1"), "Function/C1") if self.dictionary.get("C1") is not None else [1.0]
        if len(c0) != len(c1):
            raise PDFFunctionError("FunctionType 2 C0/C1 lengths differ")
        n = _number(self.doc, self.dictionary.get("N"), "Function/N")

        def evaluate(values: list[float]) -> list[float]:
            if len(values) != 1:
                raise PDFFunctionError("FunctionType 2 requires one input")
            power = values[0] ** n
            return [c0[index] + power * (c1[index] - c0[index]) for index in range(len(c0))]

        return evaluate

    def _stitching(self):
        functions_value = resolve(self.doc, self.dictionary.get("Functions"))
        if not isinstance(functions_value, list) or not functions_value:
            raise PDFFunctionError("FunctionType 3 Functions is missing/empty")
        functions = [PDFFunction(self.doc, item) for item in functions_value]
        bounds = _numbers(self.doc, self.dictionary.get("Bounds"), "Function/Bounds")
        encode = _numbers(self.doc, self.dictionary.get("Encode"), "Function/Encode")
        if len(bounds) != len(functions) - 1 or len(encode) != len(functions) * 2:
            raise PDFFunctionError("FunctionType 3 Bounds/Encode lengths are invalid")
        d0, d1 = self.domain[0], self.domain[1]
        cuts = [d0, *bounds, d1]
        if any(right < left for left, right in zip(cuts, cuts[1:])):
            raise PDFFunctionError("FunctionType 3 Bounds are not monotonic")

        def evaluate(values: list[float]) -> list[float]:
            if len(values) != 1:
                raise PDFFunctionError("FunctionType 3 requires one input")
            x = values[0]
            index = len(functions) - 1
            for candidate in range(len(functions)):
                if x < cuts[candidate + 1] or candidate == len(functions) - 1:
                    index = candidate
                    break
            mapped = _interpolate(
                x,
                cuts[index],
                cuts[index + 1],
                encode[index * 2],
                encode[index * 2 + 1],
            )
            return functions[index].evaluate([mapped])

        return evaluate

    def _sampled(self):
        if self.stream is None:
            raise PDFFunctionError("FunctionType 0 shall be a stream")
        size = _ints(self.doc, self.dictionary.get("Size"), "Function/Size")
        input_count = len(self.domain) // 2
        if len(size) != input_count or any(value <= 0 for value in size):
            raise PDFFunctionError("FunctionType 0 Size does not match Domain")
        bits = int(_number(self.doc, self.dictionary.get("BitsPerSample"), "Function/BitsPerSample"))
        if bits not in (1, 2, 4, 8, 12, 16, 24, 32):
            raise PDFFunctionError(f"unsupported sampled BitsPerSample {bits}")
        order = int(_number(self.doc, self.dictionary.get("Order"), "Function/Order")) if self.dictionary.get("Order") is not None else 1
        if order not in (1, 3):
            raise PDFFunctionError("sampled function Order must be 1 or 3")
        encode = _numbers(self.doc, self.dictionary.get("Encode"), "Function/Encode") if self.dictionary.get("Encode") is not None else [value for extent in size for value in (0.0, float(extent - 1))]
        if len(encode) != input_count * 2:
            raise PDFFunctionError("sampled Function Encode length is invalid")
        if self.range is None:
            raise PDFFunctionError("FunctionType 0 requires Range to determine outputs")
        output_count = len(self.range) // 2
        decode = _numbers(self.doc, self.dictionary.get("Decode"), "Function/Decode") if self.dictionary.get("Decode") is not None else list(self.range)
        if len(decode) != output_count * 2:
            raise PDFFunctionError("sampled Function Decode length is invalid")
        raw = decoded_stream_bytes(self.doc, self.stream, label="sampled PDF function")
        sample_count = math.prod(size) * output_count
        expected_bits = sample_count * bits
        if len(raw) * 8 < expected_bits:
            raise PDFFunctionError("sampled function stream is truncated")
        samples = _unpack_bits(raw, sample_count, bits)
        maximum = (1 << bits) - 1

        def sample_at(indices: list[int], channel: int) -> float:
            flat = 0
            for axis, index in enumerate(indices):
                flat = flat * size[axis] + index
            raw_value = samples[flat * output_count + channel]
            return _interpolate(
                raw_value,
                0,
                maximum,
                decode[channel * 2],
                decode[channel * 2 + 1],
            )

        def evaluate(values: list[float]) -> list[float]:
            coords = [
                _clip(
                    _interpolate(
                        values[axis],
                        self.domain[axis * 2],
                        self.domain[axis * 2 + 1],
                        encode[axis * 2],
                        encode[axis * 2 + 1],
                    ),
                    0,
                    size[axis] - 1,
                )
                for axis in range(input_count)
            ]
            if order == 1:
                return _multilinear(coords, size, output_count, sample_at)
            return _cubic_tensor(coords, size, output_count, sample_at)

        return evaluate

    def _calculator(self):
        if self.stream is None:
            raise PDFFunctionError("FunctionType 4 shall be a stream")
        program = _parse_calculator(decoded_stream_bytes(self.doc, self.stream, label="calculator PDF function"))
        output_count = len(self.range) // 2 if self.range is not None else None

        def evaluate(values: list[float]) -> list[float]:
            stack: list[object] = [float(value) for value in values]
            _execute(program, stack, depth=0, budget=[100_000])
            if output_count is None:
                if not stack:
                    raise PDFFunctionError("calculator function produced no output")
                return [float(value) for value in stack if isinstance(value, (int, float)) and not isinstance(value, bool)]
            if len(stack) < output_count:
                raise PDFFunctionError("calculator function stack has too few outputs")
            result = stack[-output_count:]
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in result):
                raise PDFFunctionError("calculator function outputs are not numeric")
            return [float(value) for value in result]

        return evaluate


def _unpack_bits(data: bytes, count: int, bits: int) -> list[int]:
    values: list[int] = []
    bit_position = 0
    for _ in range(count):
        value = 0
        remaining = bits
        while remaining:
            byte_index = bit_position // 8
            offset = bit_position % 8
            available = 8 - offset
            take = min(available, remaining)
            shift = available - take
            piece = (data[byte_index] >> shift) & ((1 << take) - 1)
            value = (value << take) | piece
            bit_position += take
            remaining -= take
        values.append(value)
    return values


def _multilinear(coords, size, outputs, sample_at):
    lows = [int(math.floor(value)) for value in coords]
    highs = [min(size[index] - 1, lows[index] + 1) for index in range(len(coords))]
    fractions = [value - low for value, low in zip(coords, lows)]
    result = [0.0] * outputs
    for mask in range(1 << len(coords)):
        indices = []
        weight = 1.0
        for axis in range(len(coords)):
            if mask & (1 << axis):
                indices.append(highs[axis])
                weight *= fractions[axis]
            else:
                indices.append(lows[axis])
                weight *= 1 - fractions[axis]
        if weight == 0:
            continue
        for channel in range(outputs):
            result[channel] += weight * sample_at(indices, channel)
    return result


def _cubic_weights(t: float) -> tuple[float, float, float, float]:
    # Catmull-Rom cubic interpolation used as a smooth order-3 approximation.
    return (
        -0.5 * t + t * t - 0.5 * t * t * t,
        1 - 2.5 * t * t + 1.5 * t * t * t,
        0.5 * t + 2 * t * t - 1.5 * t * t * t,
        -0.5 * t * t + 0.5 * t * t * t,
    )


def _cubic_tensor(coords, size, outputs, sample_at):
    bases = [int(math.floor(value)) for value in coords]
    weights = [_cubic_weights(value - base) for value, base in zip(coords, bases)]
    result = [0.0] * outputs

    def recurse(axis: int, indices: list[int], weight: float) -> None:
        if axis == len(coords):
            for channel in range(outputs):
                result[channel] += weight * sample_at(indices, channel)
            return
        for offset in range(4):
            index = max(0, min(size[axis] - 1, bases[axis] + offset - 1))
            recurse(axis + 1, [*indices, index], weight * weights[axis][offset])

    recurse(0, [], 1.0)
    return result


# --- Type 4 calculator language -------------------------------------------------

_TOKEN = re.compile(
    rb"%[^\r\n]*|\{|\}|-?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[Ee][+-]?\d+)?|true|false|[A-Za-z][A-Za-z0-9]*"
)


def _parse_calculator(data: bytes):
    tokens = [token for token in _TOKEN.findall(data) if not token.startswith(b"%")]
    index = 0

    def parse_block(expect_close: bool = False):
        nonlocal index
        block = []
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == b"}":
                if not expect_close:
                    raise PDFFunctionError("unexpected } in calculator function")
                return block
            if token == b"{":
                block.append(("proc", parse_block(True)))
                continue
            text = token.decode("ascii")
            if text == "true":
                block.append(True)
            elif text == "false":
                block.append(False)
            else:
                try:
                    value = float(text) if any(char in text for char in ".Ee") else int(text)
                except ValueError:
                    block.append(("op", text))
                else:
                    block.append(value)
        if expect_close:
            raise PDFFunctionError("unterminated { in calculator function")
        return block

    program = parse_block(False)
    if len(program) == 1 and isinstance(program[0], tuple) and program[0][0] == "proc":
        program = program[0][1]
    return program


def _pop_number(stack):
    if not stack:
        raise PDFFunctionError("calculator stack underflow")
    value = stack.pop()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PDFFunctionError("calculator expected numeric operand")
    return value


def _pop_int(stack):
    value = _pop_number(stack)
    integer = int(value)
    if integer != value:
        raise PDFFunctionError("calculator expected integer operand")
    return integer


def _execute(program, stack, *, depth: int, budget: list[int]):
    if depth > 64:
        raise PDFFunctionError("calculator procedure nesting exceeds limit")
    for token in program:
        budget[0] -= 1
        if budget[0] < 0:
            raise PDFFunctionError("calculator execution budget exceeded")
        if not (isinstance(token, tuple) and token and token[0] == "op"):
            stack.append(token)
            continue
        op = token[1]
        if op in {"add", "sub", "mul", "div", "idiv", "mod"}:
            b = _pop_number(stack); a = _pop_number(stack)
            if op == "add": stack.append(a + b)
            elif op == "sub": stack.append(a - b)
            elif op == "mul": stack.append(a * b)
            elif op == "div": stack.append(a / b)
            elif op == "idiv": stack.append(int(a / b))
            else: stack.append(int(a) % int(b))
        elif op in {"neg", "abs", "ceiling", "floor", "round", "truncate", "sqrt", "sin", "cos", "atan", "exp", "ln", "log", "cvi", "cvr"}:
            a = _pop_number(stack)
            if op == "neg": stack.append(-a)
            elif op == "abs": stack.append(abs(a))
            elif op == "ceiling": stack.append(math.ceil(a))
            elif op == "floor": stack.append(math.floor(a))
            elif op == "round": stack.append(round(a))
            elif op == "truncate": stack.append(math.trunc(a))
            elif op == "sqrt": stack.append(math.sqrt(a))
            elif op == "sin": stack.append(math.sin(math.radians(a)))
            elif op == "cos": stack.append(math.cos(math.radians(a)))
            elif op == "atan":
                b = _pop_number(stack)
                stack.append(math.degrees(math.atan2(b, a)) % 360.0)
            elif op == "exp":
                b = _pop_number(stack)
                stack.append(a ** b)
            elif op == "ln": stack.append(math.log(a))
            elif op == "log": stack.append(math.log10(a))
            elif op == "cvi": stack.append(int(a))
            else: stack.append(float(a))
        elif op in {"eq", "ne", "gt", "ge", "lt", "le"}:
            if len(stack) < 2: raise PDFFunctionError("calculator stack underflow")
            b = stack.pop(); a = stack.pop()
            if op == "eq": stack.append(a == b)
            elif op == "ne": stack.append(a != b)
            elif op == "gt": stack.append(a > b)
            elif op == "ge": stack.append(a >= b)
            elif op == "lt": stack.append(a < b)
            else: stack.append(a <= b)
        elif op in {"and", "or", "xor"}:
            if len(stack) < 2: raise PDFFunctionError("calculator stack underflow")
            b = stack.pop(); a = stack.pop()
            if isinstance(a, bool) and isinstance(b, bool):
                stack.append(a and b if op == "and" else a or b if op == "or" else bool(a) ^ bool(b))
            elif isinstance(a, int) and isinstance(b, int):
                stack.append(a & b if op == "and" else a | b if op == "or" else a ^ b)
            else:
                raise PDFFunctionError("calculator and/or/xor operand type mismatch")
        elif op == "not":
            if not stack: raise PDFFunctionError("calculator stack underflow")
            a = stack.pop()
            stack.append(not a if isinstance(a, bool) else ~a if isinstance(a, int) else (_ for _ in ()).throw(PDFFunctionError("calculator not expects bool/int")))
        elif op in {"bitshift"}:
            shift = _pop_int(stack); value = _pop_int(stack)
            stack.append(value << shift if shift >= 0 else value >> -shift)
        elif op == "dup":
            if not stack: raise PDFFunctionError("calculator stack underflow")
            stack.append(stack[-1])
        elif op == "exch":
            if len(stack) < 2: raise PDFFunctionError("calculator stack underflow")
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op == "pop":
            if not stack: raise PDFFunctionError("calculator stack underflow")
            stack.pop()
        elif op == "copy":
            count = _pop_int(stack)
            if count < 0 or count > len(stack): raise PDFFunctionError("calculator copy count invalid")
            stack.extend(stack[-count:] if count else [])
        elif op == "index":
            index = _pop_int(stack)
            if index < 0 or index >= len(stack): raise PDFFunctionError("calculator index invalid")
            stack.append(stack[-index - 1])
        elif op == "roll":
            shift = _pop_int(stack); count = _pop_int(stack)
            if count < 0 or count > len(stack): raise PDFFunctionError("calculator roll count invalid")
            if count:
                shift %= count
                stack[-count:] = stack[-shift:] + stack[-count:-shift] if shift else stack[-count:]
        elif op in {"if", "ifelse"}:
            if op == "if":
                if len(stack) < 2: raise PDFFunctionError("calculator stack underflow")
                proc = stack.pop(); condition = stack.pop()
                if not isinstance(condition, bool) or not (isinstance(proc, tuple) and proc[0] == "proc"):
                    raise PDFFunctionError("calculator if expects bool procedure")
                if condition: _execute(proc[1], stack, depth=depth + 1, budget=budget)
            else:
                if len(stack) < 3: raise PDFFunctionError("calculator stack underflow")
                false_proc = stack.pop(); true_proc = stack.pop(); condition = stack.pop()
                if not isinstance(condition, bool) or not all(isinstance(p, tuple) and p[0] == "proc" for p in (true_proc, false_proc)):
                    raise PDFFunctionError("calculator ifelse expects bool procedures")
                _execute((true_proc if condition else false_proc)[1], stack, depth=depth + 1, budget=budget)
        elif op in {"min", "max"}:
            b = _pop_number(stack); a = _pop_number(stack)
            stack.append(min(a, b) if op == "min" else max(a, b))
        else:
            raise PDFFunctionError(f"unsupported calculator operator {op!r}")
        if len(stack) > 10_000:
            raise PDFFunctionError("calculator stack exceeds safety limit")
