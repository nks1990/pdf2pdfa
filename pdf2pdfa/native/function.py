"""Strict owned evaluator for PDF FunctionType 0, 2, 3 and 4.

Correctness is preferred over approximation. Sampled functions support the
linear interpolation mode (Order 1). Order 3 is rejected until the exact cubic
spline algorithm is implemented and independently vector-tested.
"""

from __future__ import annotations

from decimal import Decimal
import math
import re
from typing import Iterable

from .document import PDFDocument
from .objects import PDFDict, PDFObject, PDFStream
from .structure import decoded_stream_bytes, resolve


class FunctionError(ValueError):
    pass


class UnsupportedFunctionError(FunctionError):
    pass


def _number(doc: PDFDocument, value: PDFObject | None, label: str) -> float:
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise FunctionError(f"{label} contains a non-number")
    return float(value)


def _numbers(doc: PDFDocument, value: PDFObject | None, label: str) -> list[float]:
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise FunctionError(f"{label} is not an array")
    return [_number(doc, item, label) for item in value]


def _integers(doc: PDFDocument, value: PDFObject | None, label: str) -> list[int]:
    output: list[int] = []
    for value in _numbers(doc, value, label):
        integer = int(value)
        if integer != value:
            raise FunctionError(f"{label} contains a non-integer")
        output.append(integer)
    return output


def _clip(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


def _interpolate(x: float, xmin: float, xmax: float, ymin: float, ymax: float) -> float:
    if xmax == xmin:
        return ymin
    return ymin + (x - xmin) * (ymax - ymin) / (xmax - xmin)


def _domain_clip(values: Iterable[float], domain: list[float]) -> list[float]:
    values = list(values)
    if len(domain) != 2 * len(values):
        raise FunctionError(
            f"function expects {len(domain) // 2} inputs, got {len(values)}"
        )
    return [
        _clip(value, domain[2 * index], domain[2 * index + 1])
        for index, value in enumerate(values)
    ]


def _range_clip(values: list[float], bounds: list[float] | None) -> list[float]:
    if bounds is None:
        return values
    if len(bounds) != 2 * len(values):
        raise FunctionError("function Range length does not match outputs")
    return [
        _clip(value, bounds[2 * index], bounds[2 * index + 1])
        for index, value in enumerate(values)
    ]


def _unpack_samples(data: bytes, count: int, bits: int) -> list[int]:
    required_bits = count * bits
    if required_bits > len(data) * 8:
        raise FunctionError("sampled function stream is truncated")
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


class PDFFunction:
    def __init__(self, doc: PDFDocument, value: PDFObject) -> None:
        self.doc = doc
        resolved = resolve(doc, value)
        self.stream = resolved if isinstance(resolved, PDFStream) else None
        self.dictionary = (
            resolved.dictionary if isinstance(resolved, PDFStream) else resolved
        )
        if not isinstance(self.dictionary, PDFDict):
            raise FunctionError("PDF function is not a dictionary/stream")
        raw_type = resolve(doc, self.dictionary.get("FunctionType"))
        if isinstance(raw_type, bool) or not isinstance(raw_type, int):
            raise FunctionError("FunctionType is missing or invalid")
        self.function_type = raw_type
        self.domain = _numbers(doc, self.dictionary.get("Domain"), "Function/Domain")
        if not self.domain or len(self.domain) % 2:
            raise FunctionError("Function Domain is empty or malformed")
        raw_range = self.dictionary.get("Range")
        self.range = (
            _numbers(doc, raw_range, "Function/Range")
            if raw_range is not None
            else None
        )
        if self.range is not None and (not self.range or len(self.range) % 2):
            raise FunctionError("Function Range is malformed")
        self._evaluate = self._build()

    @property
    def inputs(self) -> int:
        return len(self.domain) // 2

    def evaluate(self, values: Iterable[float]) -> list[float]:
        clipped = _domain_clip(values, self.domain)
        result = list(self._evaluate(clipped))
        return _range_clip(result, self.range)

    def _build(self):
        if self.function_type == 0:
            return self._build_sampled()
        if self.function_type == 2:
            return self._build_exponential()
        if self.function_type == 3:
            return self._build_stitching()
        if self.function_type == 4:
            return self._build_calculator()
        raise UnsupportedFunctionError(
            f"unsupported PDF FunctionType {self.function_type}"
        )

    def _build_exponential(self):
        if self.inputs != 1:
            raise FunctionError("FunctionType 2 requires exactly one input")
        c0 = (
            _numbers(self.doc, self.dictionary.get("C0"), "Function/C0")
            if self.dictionary.get("C0") is not None
            else [0.0]
        )
        c1 = (
            _numbers(self.doc, self.dictionary.get("C1"), "Function/C1")
            if self.dictionary.get("C1") is not None
            else [1.0]
        )
        if len(c0) != len(c1):
            raise FunctionError("FunctionType 2 C0/C1 lengths differ")
        exponent = _number(self.doc, self.dictionary.get("N"), "Function/N")

        def evaluate(values: list[float]) -> list[float]:
            power = values[0] ** exponent
            return [
                c0[index] + power * (c1[index] - c0[index])
                for index in range(len(c0))
            ]

        return evaluate

    def _build_stitching(self):
        if self.inputs != 1:
            raise FunctionError("FunctionType 3 requires exactly one input")
        raw_functions = resolve(self.doc, self.dictionary.get("Functions"))
        if not isinstance(raw_functions, list) or not raw_functions:
            raise FunctionError("FunctionType 3 has no component Functions")
        functions = [PDFFunction(self.doc, item) for item in raw_functions]
        bounds = _numbers(self.doc, self.dictionary.get("Bounds"), "Function/Bounds")
        encode = _numbers(self.doc, self.dictionary.get("Encode"), "Function/Encode")
        if len(bounds) != len(functions) - 1:
            raise FunctionError("FunctionType 3 Bounds length is invalid")
        if len(encode) != 2 * len(functions):
            raise FunctionError("FunctionType 3 Encode length is invalid")
        cuts = [self.domain[0], *bounds, self.domain[1]]
        if any(right < left for left, right in zip(cuts, cuts[1:])):
            raise FunctionError("FunctionType 3 bounds are not monotonic")

        def evaluate(values: list[float]) -> list[float]:
            x = values[0]
            selected = len(functions) - 1
            for index in range(len(functions)):
                if x < cuts[index + 1] or index == len(functions) - 1:
                    selected = index
                    break
            mapped = _interpolate(
                x,
                cuts[selected],
                cuts[selected + 1],
                encode[2 * selected],
                encode[2 * selected + 1],
            )
            return functions[selected].evaluate([mapped])

        return evaluate

    def _build_sampled(self):
        if self.stream is None:
            raise FunctionError("FunctionType 0 shall be a stream")
        if self.range is None:
            raise FunctionError("FunctionType 0 requires Range")
        size = _integers(self.doc, self.dictionary.get("Size"), "Function/Size")
        if len(size) != self.inputs or any(value <= 0 for value in size):
            raise FunctionError("FunctionType 0 Size does not match input count")
        bits = int(
            _number(
                self.doc,
                self.dictionary.get("BitsPerSample"),
                "Function/BitsPerSample",
            )
        )
        if bits not in (1, 2, 4, 8, 12, 16, 24, 32):
            raise UnsupportedFunctionError(
                f"unsupported sampled BitsPerSample {bits}"
            )
        order = (
            int(_number(self.doc, self.dictionary.get("Order"), "Function/Order"))
            if self.dictionary.get("Order") is not None
            else 1
        )
        if order == 3:
            raise UnsupportedFunctionError(
                "FunctionType 0 Order 3 is rejected until the exact PDF cubic "
                "spline interpolation algorithm is implemented and vector-tested"
            )
        if order != 1:
            raise UnsupportedFunctionError(
                f"unsupported FunctionType 0 interpolation Order {order}"
            )
        encode = (
            _numbers(self.doc, self.dictionary.get("Encode"), "Function/Encode")
            if self.dictionary.get("Encode") is not None
            else [
                component
                for extent in size
                for component in (0.0, float(extent - 1))
            ]
        )
        if len(encode) != 2 * self.inputs:
            raise FunctionError("FunctionType 0 Encode length is invalid")
        output_count = len(self.range) // 2
        decode = (
            _numbers(self.doc, self.dictionary.get("Decode"), "Function/Decode")
            if self.dictionary.get("Decode") is not None
            else list(self.range)
        )
        if len(decode) != 2 * output_count:
            raise FunctionError("FunctionType 0 Decode length is invalid")
        raw = decoded_stream_bytes(
            self.doc, self.stream, label="sampled PDF function"
        )
        sample_count = math.prod(size) * output_count
        samples = _unpack_samples(raw, sample_count, bits)
        maximum = (1 << bits) - 1

        def at(indices: list[int], channel: int) -> float:
            flat = 0
            for axis, index in enumerate(indices):
                flat = flat * size[axis] + index
            sample = samples[flat * output_count + channel]
            return _interpolate(
                sample,
                0,
                maximum,
                decode[2 * channel],
                decode[2 * channel + 1],
            )

        def evaluate(values: list[float]) -> list[float]:
            coordinates = [
                _clip(
                    _interpolate(
                        values[axis],
                        self.domain[2 * axis],
                        self.domain[2 * axis + 1],
                        encode[2 * axis],
                        encode[2 * axis + 1],
                    ),
                    0.0,
                    float(size[axis] - 1),
                )
                for axis in range(self.inputs)
            ]
            lows = [int(math.floor(value)) for value in coordinates]
            highs = [
                min(size[axis] - 1, lows[axis] + 1)
                for axis in range(self.inputs)
            ]
            fractions = [
                coordinates[axis] - lows[axis] for axis in range(self.inputs)
            ]
            result = [0.0] * output_count
            for corner in range(1 << self.inputs):
                weight = 1.0
                indices: list[int] = []
                for axis in range(self.inputs):
                    if corner & (1 << axis):
                        indices.append(highs[axis])
                        weight *= fractions[axis]
                    else:
                        indices.append(lows[axis])
                        weight *= 1.0 - fractions[axis]
                if weight == 0.0:
                    continue
                for channel in range(output_count):
                    result[channel] += weight * at(indices, channel)
            return result

        return evaluate

    def _build_calculator(self):
        if self.stream is None:
            raise FunctionError("FunctionType 4 shall be a stream")
        if self.range is None:
            raise FunctionError(
                "FunctionType 4 requires Range so output arity is unambiguous"
            )
        program = _parse_program(
            decoded_stream_bytes(
                self.doc, self.stream, label="calculator PDF function"
            )
        )
        output_count = len(self.range) // 2

        def evaluate(values: list[float]) -> list[float]:
            stack: list[object] = [float(value) for value in values]
            budget = [100_000]
            _execute(program, stack, depth=0, budget=budget)
            if len(stack) < output_count:
                raise FunctionError("calculator function produced too few outputs")
            output = stack[-output_count:]
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in output
            ):
                raise FunctionError("calculator function output is non-numeric")
            return [float(value) for value in output]

        return evaluate


_TOKEN = re.compile(
    rb"%[^\r\n]*|\{|\}|-?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[Ee][+-]?\d+)?|true|false|[A-Za-z][A-Za-z0-9]*"
)


def _parse_program(data: bytes):
    tokens = [token for token in _TOKEN.findall(data) if not token.startswith(b"%")]
    index = 0

    def parse_block(expect_close: bool):
        nonlocal index
        block: list[object] = []
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == b"}":
                if not expect_close:
                    raise FunctionError("unexpected } in calculator function")
                return block
            if token == b"{":
                block.append(("proc", parse_block(True)))
                continue
            text = token.decode("ascii")
            if text == "true":
                block.append(True)
                continue
            if text == "false":
                block.append(False)
                continue
            try:
                number = (
                    float(text)
                    if any(character in text for character in ".Ee")
                    else int(text)
                )
            except ValueError:
                block.append(("op", text))
            else:
                block.append(number)
        if expect_close:
            raise FunctionError("unterminated calculator procedure")
        return block

    program = parse_block(False)
    if (
        len(program) == 1
        and isinstance(program[0], tuple)
        and program[0][0] == "proc"
    ):
        program = program[0][1]
    return program


def _pop(stack: list[object]) -> object:
    if not stack:
        raise FunctionError("calculator stack underflow")
    return stack.pop()


def _pop_number(stack: list[object]) -> float | int:
    value = _pop(stack)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FunctionError("calculator expected numeric operand")
    return value


def _pop_int(stack: list[object]) -> int:
    value = _pop_number(stack)
    integer = int(value)
    if integer != value:
        raise FunctionError("calculator expected integer operand")
    return integer


def _pop_proc(stack: list[object]):
    value = _pop(stack)
    if not (isinstance(value, tuple) and len(value) == 2 and value[0] == "proc"):
        raise FunctionError("calculator expected procedure operand")
    return value[1]


def _execute(program, stack: list[object], *, depth: int, budget: list[int]) -> None:
    if depth > 64:
        raise FunctionError("calculator procedure nesting exceeds 64")
    for token in program:
        budget[0] -= 1
        if budget[0] < 0:
            raise FunctionError("calculator execution budget exceeded")
        if not (
            isinstance(token, tuple)
            and len(token) == 2
            and token[0] == "op"
        ):
            stack.append(token)
            continue
        op = token[1]
        if op == "add":
            b, a = _pop_number(stack), _pop_number(stack); stack.append(a + b)
        elif op == "sub":
            b, a = _pop_number(stack), _pop_number(stack); stack.append(a - b)
        elif op == "mul":
            b, a = _pop_number(stack), _pop_number(stack); stack.append(a * b)
        elif op == "div":
            b, a = _pop_number(stack), _pop_number(stack); stack.append(a / b)
        elif op == "idiv":
            b, a = _pop_number(stack), _pop_number(stack); stack.append(int(a / b))
        elif op == "mod":
            b, a = _pop_int(stack), _pop_int(stack); stack.append(a % b)
        elif op == "neg":
            stack.append(-_pop_number(stack))
        elif op == "abs":
            stack.append(abs(_pop_number(stack)))
        elif op == "ceiling":
            stack.append(math.ceil(_pop_number(stack)))
        elif op == "floor":
            stack.append(math.floor(_pop_number(stack)))
        elif op == "round":
            stack.append(round(_pop_number(stack)))
        elif op == "truncate":
            stack.append(math.trunc(_pop_number(stack)))
        elif op == "sqrt":
            stack.append(math.sqrt(_pop_number(stack)))
        elif op == "sin":
            stack.append(math.sin(math.radians(_pop_number(stack))))
        elif op == "cos":
            stack.append(math.cos(math.radians(_pop_number(stack))))
        elif op == "atan":
            denominator = _pop_number(stack)
            numerator = _pop_number(stack)
            stack.append(math.degrees(math.atan2(numerator, denominator)) % 360.0)
        elif op == "exp":
            exponent = _pop_number(stack)
            base = _pop_number(stack)
            stack.append(base ** exponent)
        elif op == "ln":
            stack.append(math.log(_pop_number(stack)))
        elif op == "log":
            stack.append(math.log10(_pop_number(stack)))
        elif op == "cvi":
            stack.append(int(_pop_number(stack)))
        elif op == "cvr":
            stack.append(float(_pop_number(stack)))
        elif op in {"eq", "ne", "gt", "ge", "lt", "le"}:
            right, left = _pop(stack), _pop(stack)
            if op == "eq": stack.append(left == right)
            elif op == "ne": stack.append(left != right)
            elif op == "gt": stack.append(left > right)
            elif op == "ge": stack.append(left >= right)
            elif op == "lt": stack.append(left < right)
            else: stack.append(left <= right)
        elif op in {"and", "or", "xor"}:
            right, left = _pop(stack), _pop(stack)
            if isinstance(left, bool) and isinstance(right, bool):
                if op == "and": stack.append(left and right)
                elif op == "or": stack.append(left or right)
                else: stack.append(left ^ right)
            elif isinstance(left, int) and isinstance(right, int):
                if op == "and": stack.append(left & right)
                elif op == "or": stack.append(left | right)
                else: stack.append(left ^ right)
            else:
                raise FunctionError("calculator and/or/xor operand types differ")
        elif op == "not":
            value = _pop(stack)
            if isinstance(value, bool):
                stack.append(not value)
            elif isinstance(value, int):
                stack.append(~value)
            else:
                raise FunctionError("calculator not expects bool or integer")
        elif op == "bitshift":
            shift = _pop_int(stack)
            value = _pop_int(stack)
            stack.append(value << shift if shift >= 0 else value >> -shift)
        elif op == "dup":
            if not stack: raise FunctionError("calculator stack underflow")
            stack.append(stack[-1])
        elif op == "exch":
            if len(stack) < 2: raise FunctionError("calculator stack underflow")
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op == "pop":
            _pop(stack)
        elif op == "copy":
            count = _pop_int(stack)
            if count < 0 or count > len(stack):
                raise FunctionError("calculator copy count is invalid")
            if count:
                stack.extend(stack[-count:])
        elif op == "index":
            index = _pop_int(stack)
            if index < 0 or index >= len(stack):
                raise FunctionError("calculator index is invalid")
            stack.append(stack[-index - 1])
        elif op == "roll":
            shift = _pop_int(stack)
            count = _pop_int(stack)
            if count < 0 or count > len(stack):
                raise FunctionError("calculator roll count is invalid")
            if count:
                shift %= count
                if shift:
                    segment = stack[-count:]
                    stack[-count:] = segment[-shift:] + segment[:-shift]
        elif op == "if":
            procedure = _pop_proc(stack)
            condition = _pop(stack)
            if not isinstance(condition, bool):
                raise FunctionError("calculator if expects a boolean")
            if condition:
                _execute(procedure, stack, depth=depth + 1, budget=budget)
        elif op == "ifelse":
            false_procedure = _pop_proc(stack)
            true_procedure = _pop_proc(stack)
            condition = _pop(stack)
            if not isinstance(condition, bool):
                raise FunctionError("calculator ifelse expects a boolean")
            _execute(
                true_procedure if condition else false_procedure,
                stack,
                depth=depth + 1,
                budget=budget,
            )
        else:
            raise UnsupportedFunctionError(
                f"unsupported calculator operator {op!r}"
            )
        if len(stack) > 10_000:
            raise FunctionError("calculator stack exceeds safety limit")
