"""Pure-Python JPEG decoder for PDF DCTDecode image XObjects.

Implements baseline sequential Huffman JPEG with grayscale, YCbCr/RGB and
CMYK/YCCK output, restart intervals, 8/16-bit quantization tables and arbitrary
sampling factors. Progressive/lossless JPEG is detected explicitly and never
silently mis-decoded.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterator


class JPEGError(ValueError):
    pass


class UnsupportedJPEGError(JPEGError):
    pass


_ZIGZAG = (
    0,1,8,16,9,2,3,10,
    17,24,32,25,18,11,4,5,
    12,19,26,33,40,48,41,34,
    27,20,13,6,7,14,21,28,
    35,42,49,56,57,50,43,36,
    29,22,15,23,30,37,44,51,
    58,59,52,45,38,31,39,46,
    53,60,61,54,47,55,62,63,
)

_COS = tuple(
    tuple(math.cos(((2 * x + 1) * u * math.pi) / 16.0) for u in range(8))
    for x in range(8)
)
_INV_SQRT2 = 1.0 / math.sqrt(2.0)


@dataclass(slots=True)
class _Component:
    identifier: int
    h: int
    v: int
    tq: int
    dc_table: int = 0
    ac_table: int = 0
    dc_predictor: int = 0
    plane: list[int] | None = None
    plane_width: int = 0
    plane_height: int = 0


class _Huffman:
    def __init__(self, counts: list[int], symbols: bytes) -> None:
        if len(counts) != 16 or sum(counts) != len(symbols):
            raise JPEGError("invalid JPEG Huffman table lengths")
        self.lookup: dict[tuple[int, int], int] = {}
        code = 0
        index = 0
        for length, count in enumerate(counts, start=1):
            for _ in range(count):
                self.lookup[(length, code)] = symbols[index]
                index += 1
                code += 1
            code <<= 1
        if code > (1 << 17):
            raise JPEGError("over-subscribed JPEG Huffman table")

    def decode(self, bits: "_BitReader") -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | bits.bit()
            symbol = self.lookup.get((length, code))
            if symbol is not None:
                return symbol
        raise JPEGError("invalid JPEG Huffman code")


class _Restart(Exception):
    def __init__(self, marker: int) -> None:
        self.marker = marker


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        self.buffer = 0
        self.bits = 0

    def _byte(self) -> int:
        if self.position >= len(self.data):
            raise JPEGError("unexpected end of JPEG entropy data")
        value = self.data[self.position]
        self.position += 1
        if value != 0xFF:
            return value
        while self.position < len(self.data) and self.data[self.position] == 0xFF:
            self.position += 1
        if self.position >= len(self.data):
            raise JPEGError("truncated JPEG marker in entropy data")
        marker = self.data[self.position]
        self.position += 1
        if marker == 0x00:
            return 0xFF
        if 0xD0 <= marker <= 0xD7:
            self.buffer = 0
            self.bits = 0
            raise _Restart(marker)
        raise JPEGError(f"unexpected JPEG marker FF{marker:02X} inside entropy segment")

    def bit(self) -> int:
        if self.bits == 0:
            self.buffer = self._byte()
            self.bits = 8
        self.bits -= 1
        return (self.buffer >> self.bits) & 1

    def read(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def align(self) -> None:
        self.bits = 0
        self.buffer = 0


@dataclass(frozen=True, slots=True)
class JPEGImage:
    width: int
    height: int
    mode: str
    pixels: bytes

    @property
    def components(self) -> int:
        return 1 if self.mode == "L" else 3 if self.mode == "RGB" else 4


class JPEGDecoder:
    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.width = 0
        self.height = 0
        self.precision = 0
        self.components: dict[int, _Component] = {}
        self.quant: dict[int, list[int]] = {}
        self.huffman_dc: dict[int, _Huffman] = {}
        self.huffman_ac: dict[int, _Huffman] = {}
        self.restart_interval = 0
        self.adobe_transform: int | None = None
        self.jfif = False
        self._scan_data: bytes | None = None
        self._scan_components: list[_Component] = []

    def decode(self) -> JPEGImage:
        self._parse()
        if self.precision != 8:
            raise UnsupportedJPEGError(
                f"owned JPEG decoder currently supports 8-bit samples, found {self.precision}"
            )
        if self._scan_data is None:
            raise JPEGError("JPEG contains no baseline scan")
        self._decode_baseline(self._scan_data)
        return self._compose()

    def _parse(self) -> None:
        if len(self.data) < 4 or self.data[:2] != b"\xff\xd8":
            raise JPEGError("missing JPEG SOI marker")
        position = 2
        saw_frame = False
        while position < len(self.data):
            if self.data[position] != 0xFF:
                raise JPEGError(f"expected JPEG marker at offset {position}")
            while position < len(self.data) and self.data[position] == 0xFF:
                position += 1
            if position >= len(self.data):
                raise JPEGError("truncated JPEG marker")
            marker = self.data[position]
            position += 1
            if marker == 0xD9:
                break
            if marker in range(0xD0, 0xD8) or marker == 0x01:
                continue
            if position + 2 > len(self.data):
                raise JPEGError("truncated JPEG segment length")
            length = int.from_bytes(self.data[position : position + 2], "big")
            if length < 2 or position + length > len(self.data):
                raise JPEGError(f"invalid JPEG segment length {length}")
            payload = self.data[position + 2 : position + length]
            position += length

            if marker == 0xDB:
                self._dqt(payload)
            elif marker == 0xC4:
                self._dht(payload)
            elif marker == 0xC0:
                self._sof0(payload)
                saw_frame = True
            elif marker in (0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if marker == 0xC2:
                    raise UnsupportedJPEGError(
                        "progressive JPEG (SOF2) requires the owned progressive decoder"
                    )
                raise UnsupportedJPEGError(f"unsupported JPEG frame marker FF{marker:02X}")
            elif marker == 0xDD:
                if len(payload) != 2:
                    raise JPEGError("DRI segment must contain one 16-bit interval")
                self.restart_interval = int.from_bytes(payload, "big")
            elif marker == 0xE0:
                if payload.startswith(b"JFIF\x00"):
                    self.jfif = True
            elif marker == 0xEE:
                if payload.startswith(b"Adobe") and len(payload) >= 12:
                    self.adobe_transform = payload[11]
            elif marker == 0xDA:
                if not saw_frame:
                    raise JPEGError("SOS appears before SOF")
                self._sos(payload)
                entropy_start = position
                entropy_end = self._find_entropy_end(entropy_start)
                self._scan_data = self.data[entropy_start:entropy_end]
                position = entropy_end
                # Baseline sequential must carry all components in one scan for
                # the owned decoder. Multiple non-progressive scans are rare and
                # explicitly rejected rather than partially rendered.
                return

    def _find_entropy_end(self, start: int) -> int:
        position = start
        while position + 1 < len(self.data):
            if self.data[position] != 0xFF:
                position += 1
                continue
            next_pos = position + 1
            while next_pos < len(self.data) and self.data[next_pos] == 0xFF:
                next_pos += 1
            if next_pos >= len(self.data):
                return len(self.data)
            marker = self.data[next_pos]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                position = next_pos + 1
                continue
            return position
        return len(self.data)

    def _dqt(self, payload: bytes) -> None:
        position = 0
        while position < len(payload):
            info = payload[position]
            position += 1
            precision, table_id = info >> 4, info & 0x0F
            if table_id > 3 or precision not in (0, 1):
                raise JPEGError("invalid DQT table selector/precision")
            size = 1 if precision == 0 else 2
            if position + 64 * size > len(payload):
                raise JPEGError("truncated DQT table")
            zigzag_values = []
            for _ in range(64):
                if size == 1:
                    value = payload[position]
                else:
                    value = int.from_bytes(payload[position : position + 2], "big")
                position += size
                if value == 0:
                    raise JPEGError("JPEG quantization coefficient cannot be zero")
                zigzag_values.append(value)
            natural = [0] * 64
            for zigzag_index, natural_index in enumerate(_ZIGZAG):
                natural[natural_index] = zigzag_values[zigzag_index]
            self.quant[table_id] = natural

    def _dht(self, payload: bytes) -> None:
        position = 0
        while position < len(payload):
            info = payload[position]
            position += 1
            table_class, table_id = info >> 4, info & 0x0F
            if table_class not in (0, 1) or table_id > 3:
                raise JPEGError("invalid DHT table selector")
            if position + 16 > len(payload):
                raise JPEGError("truncated DHT code-length counts")
            counts = list(payload[position : position + 16])
            position += 16
            total = sum(counts)
            if position + total > len(payload):
                raise JPEGError("truncated DHT symbols")
            table = _Huffman(counts, payload[position : position + total])
            position += total
            (self.huffman_dc if table_class == 0 else self.huffman_ac)[table_id] = table

    def _sof0(self, payload: bytes) -> None:
        if len(payload) < 6:
            raise JPEGError("truncated SOF0")
        self.precision = payload[0]
        self.height = int.from_bytes(payload[1:3], "big")
        self.width = int.from_bytes(payload[3:5], "big")
        count = payload[5]
        if self.width <= 0 or self.height <= 0 or count not in (1, 3, 4):
            raise JPEGError("invalid baseline JPEG dimensions/component count")
        if len(payload) != 6 + 3 * count:
            raise JPEGError("SOF0 component table length mismatch")
        self.components.clear()
        for index in range(count):
            base = 6 + index * 3
            identifier = payload[base]
            sampling = payload[base + 1]
            h, v = sampling >> 4, sampling & 0x0F
            tq = payload[base + 2]
            if identifier in self.components or not (1 <= h <= 4 and 1 <= v <= 4):
                raise JPEGError("invalid SOF0 component/sampling factors")
            self.components[identifier] = _Component(identifier, h, v, tq)

    def _sos(self, payload: bytes) -> None:
        if not payload:
            raise JPEGError("truncated SOS")
        count = payload[0]
        if len(payload) != 1 + 2 * count + 3:
            raise JPEGError("SOS length mismatch")
        selected: list[_Component] = []
        for index in range(count):
            base = 1 + 2 * index
            identifier = payload[base]
            selectors = payload[base + 1]
            try:
                component = self.components[identifier]
            except KeyError as exc:
                raise JPEGError(f"SOS selects unknown component {identifier}") from exc
            component.dc_table = selectors >> 4
            component.ac_table = selectors & 0x0F
            selected.append(component)
        ss, se, ahal = payload[-3], payload[-2], payload[-1]
        if (ss, se, ahal) != (0, 63, 0):
            raise UnsupportedJPEGError("non-baseline spectral/successive approximation scan")
        if len(selected) != len(self.components):
            raise UnsupportedJPEGError("multiple-scan baseline JPEG is not yet supported")
        self._scan_components = selected

    @staticmethod
    def _extend(value: int, bits: int) -> int:
        if bits == 0:
            return 0
        threshold = 1 << (bits - 1)
        if value < threshold:
            return value - ((1 << bits) - 1)
        return value

    def _decode_block(self, bits: _BitReader, component: _Component) -> list[int]:
        try:
            dc_table = self.huffman_dc[component.dc_table]
            ac_table = self.huffman_ac[component.ac_table]
            quant = self.quant[component.tq]
        except KeyError as exc:
            raise JPEGError("scan references missing Huffman/quantization table") from exc
        coefficients = [0] * 64
        category = dc_table.decode(bits)
        if category > 11:
            raise JPEGError("baseline DC category exceeds 11")
        difference = self._extend(bits.read(category), category)
        component.dc_predictor += difference
        coefficients[0] = component.dc_predictor * quant[0]
        zigzag = 1
        while zigzag < 64:
            symbol = ac_table.decode(bits)
            run, size = symbol >> 4, symbol & 0x0F
            if size == 0:
                if run == 0:
                    break
                if run == 15:
                    zigzag += 16
                    continue
                raise JPEGError("invalid baseline AC Huffman symbol")
            zigzag += run
            if zigzag >= 64:
                raise JPEGError("AC run exceeds JPEG block")
            value = self._extend(bits.read(size), size)
            natural = _ZIGZAG[zigzag]
            coefficients[natural] = value * quant[natural]
            zigzag += 1
        return coefficients

    @staticmethod
    def _idct(coefficients: list[int]) -> list[int]:
        if all(value == 0 for value in coefficients[1:]):
            dc = max(0, min(255, round(coefficients[0] / 8.0 + 128.0)))
            return [dc] * 64
        output = [0] * 64
        for y in range(8):
            for x in range(8):
                total = 0.0
                for v in range(8):
                    cv = _INV_SQRT2 if v == 0 else 1.0
                    cosy = _COS[y][v]
                    row = v * 8
                    for u in range(8):
                        coefficient = coefficients[row + u]
                        if coefficient == 0:
                            continue
                        cu = _INV_SQRT2 if u == 0 else 1.0
                        total += cu * cv * coefficient * _COS[x][u] * cosy
                output[y * 8 + x] = max(0, min(255, round(total / 4.0 + 128.0)))
        return output

    def _decode_baseline(self, entropy: bytes) -> None:
        max_h = max(component.h for component in self.components.values())
        max_v = max(component.v for component in self.components.values())
        mcus_x = (self.width + 8 * max_h - 1) // (8 * max_h)
        mcus_y = (self.height + 8 * max_v - 1) // (8 * max_v)
        for component in self.components.values():
            component.plane_width = mcus_x * component.h * 8
            component.plane_height = mcus_y * component.v * 8
            component.plane = [0] * (component.plane_width * component.plane_height)
            component.dc_predictor = 0

        bits = _BitReader(entropy)
        mcu_index = 0
        expected_restart = 0
        for my in range(mcus_y):
            for mx in range(mcus_x):
                if self.restart_interval and mcu_index and mcu_index % self.restart_interval == 0:
                    bits.align()
                    try:
                        # Force reader to consume the restart marker; a call to
                        # _byte raises _Restart instead of returning data.
                        bits._byte()
                    except _Restart as restart:
                        expected_marker = 0xD0 + expected_restart
                        if restart.marker != expected_marker:
                            raise JPEGError(
                                f"restart marker order mismatch: FF{restart.marker:02X}, expected FF{expected_marker:02X}"
                            )
                        expected_restart = (expected_restart + 1) & 7
                        for component in self.components.values():
                            component.dc_predictor = 0
                    else:
                        raise JPEGError("restart interval declared but restart marker missing")
                for component in self._scan_components:
                    for vy in range(component.v):
                        for hx in range(component.h):
                            coefficients = self._decode_block(bits, component)
                            samples = self._idct(coefficients)
                            assert component.plane is not None
                            block_x = (mx * component.h + hx) * 8
                            block_y = (my * component.v + vy) * 8
                            for row in range(8):
                                start = (block_y + row) * component.plane_width + block_x
                                component.plane[start : start + 8] = samples[row * 8 : row * 8 + 8]
                mcu_index += 1

    def _sample(self, component: _Component, x: int, y: int, max_h: int, max_v: int) -> int:
        assert component.plane is not None
        sx = min(component.plane_width - 1, (x * component.h) // max_h)
        sy = min(component.plane_height - 1, (y * component.v) // max_v)
        return component.plane[sy * component.plane_width + sx]

    @staticmethod
    def _ycbcr(y: int, cb: int, cr: int) -> tuple[int, int, int]:
        cbf = cb - 128.0
        crf = cr - 128.0
        r = y + 1.402 * crf
        g = y - 0.344136 * cbf - 0.714136 * crf
        b = y + 1.772 * cbf
        return tuple(max(0, min(255, round(value))) for value in (r, g, b))  # type: ignore[return-value]

    def _compose(self) -> JPEGImage:
        ordered = list(self.components.values())
        max_h = max(component.h for component in ordered)
        max_v = max(component.v for component in ordered)
        if len(ordered) == 1:
            pixels = bytearray(self.width * self.height)
            component = ordered[0]
            for y in range(self.height):
                for x in range(self.width):
                    pixels[y * self.width + x] = self._sample(component, x, y, max_h, max_v)
            return JPEGImage(self.width, self.height, "L", bytes(pixels))

        if len(ordered) == 3:
            pixels = bytearray(self.width * self.height * 3)
            direct_rgb = self.adobe_transform == 0 and not self.jfif
            target = 0
            for y in range(self.height):
                for x in range(self.width):
                    values = [self._sample(component, x, y, max_h, max_v) for component in ordered]
                    rgb = tuple(values) if direct_rgb else self._ycbcr(*values)
                    pixels[target : target + 3] = bytes(rgb)
                    target += 3
            return JPEGImage(self.width, self.height, "RGB", bytes(pixels))

        if len(ordered) == 4:
            pixels = bytearray(self.width * self.height * 4)
            target = 0
            ycck = self.adobe_transform == 2
            for y in range(self.height):
                for x in range(self.width):
                    values = [self._sample(component, x, y, max_h, max_v) for component in ordered]
                    if ycck:
                        r, g, b = self._ycbcr(values[0], values[1], values[2])
                        c, m, yy = 255 - r, 255 - g, 255 - b
                        k = values[3]
                        cmyk = (c, m, yy, k)
                    else:
                        # Adobe CMYK JPEG convention stores inverted samples.
                        cmyk = tuple(255 - value for value in values) if self.adobe_transform == 0 else tuple(values)
                    pixels[target : target + 4] = bytes(cmyk)
                    target += 4
            return JPEGImage(self.width, self.height, "CMYK", bytes(pixels))

        raise UnsupportedJPEGError(f"unsupported JPEG component count {len(ordered)}")


def decode_jpeg(data: bytes) -> JPEGImage:
    return JPEGDecoder(data).decode()
