"""Pure-Python baseline JPEG decoder for PDF DCTDecode images.

The decoder implements sequential Huffman JPEG with grayscale, RGB/YCbCr and
CMYK/YCCK output, sampling factors and restart intervals. Unsupported JPEG
modes fail explicitly; they are never rendered approximately.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


class JPEGError(ValueError):
    pass


class UnsupportedJPEGError(JPEGError):
    pass


_ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)
_INV_SQRT2 = 1.0 / math.sqrt(2.0)
_COS = tuple(
    tuple(math.cos((2 * x + 1) * u * math.pi / 16.0) for u in range(8))
    for x in range(8)
)


@dataclass(slots=True)
class _Component:
    identifier: int
    h: int
    v: int
    quantization: int
    dc_table: int = 0
    ac_table: int = 0
    predictor: int = 0
    plane: list[int] | None = None
    plane_width: int = 0
    plane_height: int = 0


class _Huffman:
    def __init__(self, counts: bytes, symbols: bytes) -> None:
        if len(counts) != 16 or sum(counts) != len(symbols):
            raise JPEGError("invalid JPEG Huffman table")
        self.lookup: dict[tuple[int, int], int] = {}
        code = 0
        index = 0
        for length, count in enumerate(counts, start=1):
            for _ in range(count):
                if code >= (1 << length):
                    raise JPEGError("over-subscribed JPEG Huffman table")
                self.lookup[(length, code)] = symbols[index]
                index += 1
                code += 1
            code <<= 1

    def decode(self, reader: "_BitReader") -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | reader.bit()
            value = self.lookup.get((length, code))
            if value is not None:
                return value
        raise JPEGError("invalid JPEG Huffman code")


class _Restart(Exception):
    def __init__(self, marker: int) -> None:
        super().__init__(marker)
        self.marker = marker


class _Marker(Exception):
    def __init__(self, marker: int) -> None:
        super().__init__(marker)
        self.marker = marker


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        self.buffer = 0
        self.available = 0

    def _entropy_byte(self) -> int:
        if self.position >= len(self.data):
            raise JPEGError("unexpected end of JPEG entropy data")
        value = self.data[self.position]
        self.position += 1
        if value != 0xFF:
            return value
        while self.position < len(self.data) and self.data[self.position] == 0xFF:
            self.position += 1
        if self.position >= len(self.data):
            raise JPEGError("truncated JPEG entropy marker")
        marker = self.data[self.position]
        self.position += 1
        if marker == 0x00:
            return 0xFF
        self.available = 0
        self.buffer = 0
        if 0xD0 <= marker <= 0xD7:
            raise _Restart(marker)
        raise _Marker(marker)

    def bit(self) -> int:
        if self.available == 0:
            self.buffer = self._entropy_byte()
            self.available = 8
        self.available -= 1
        return (self.buffer >> self.available) & 1

    def read(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def align(self) -> None:
        self.buffer = 0
        self.available = 0

    def consume_restart(self, expected: int) -> None:
        self.align()
        try:
            self._entropy_byte()
        except _Restart as restart:
            if restart.marker != expected:
                raise JPEGError(
                    f"restart marker FF{restart.marker:02X} does not match "
                    f"expected FF{expected:02X}"
                )
            return
        except _Marker as marker:
            raise JPEGError(
                f"expected restart marker, found FF{marker.marker:02X}"
            ) from marker
        raise JPEGError("restart interval declared but restart marker is missing")


@dataclass(frozen=True, slots=True)
class JPEGImage:
    width: int
    height: int
    mode: str
    pixels: bytes

    @property
    def components(self) -> int:
        return {"L": 1, "RGB": 3, "CMYK": 4}[self.mode]


class JPEGDecoder:
    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.width = 0
        self.height = 0
        self.precision = 0
        self.components: dict[int, _Component] = {}
        self.quantization: dict[int, list[int]] = {}
        self.dc_tables: dict[int, _Huffman] = {}
        self.ac_tables: dict[int, _Huffman] = {}
        self.restart_interval = 0
        self.adobe_transform: int | None = None
        self.jfif = False
        self.scan_components: list[_Component] = []
        self.entropy = b""

    def decode(self) -> JPEGImage:
        self._parse()
        if self.precision != 8:
            raise UnsupportedJPEGError(
                f"only 8-bit baseline JPEG is supported, found {self.precision}-bit"
            )
        if not self.entropy:
            raise JPEGError("JPEG contains no entropy scan")
        self._decode_scan()
        return self._compose()

    def _parse(self) -> None:
        if len(self.data) < 4 or self.data[:2] != b"\xff\xd8":
            raise JPEGError("missing JPEG SOI")
        position = 2
        frame_seen = False
        scan_seen = False
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
                return
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            if position + 2 > len(self.data):
                raise JPEGError("truncated JPEG segment length")
            length = int.from_bytes(self.data[position : position + 2], "big")
            if length < 2 or position + length > len(self.data):
                raise JPEGError("invalid JPEG segment length")
            payload = self.data[position + 2 : position + length]
            position += length

            if marker == 0xDB:
                self._parse_dqt(payload)
            elif marker == 0xC4:
                self._parse_dht(payload)
            elif marker == 0xC0:
                if frame_seen:
                    raise JPEGError("multiple SOF0 frames are not supported")
                self._parse_sof0(payload)
                frame_seen = True
            elif marker in {
                0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                if marker == 0xC2:
                    raise UnsupportedJPEGError(
                        "progressive JPEG requires the owned progressive decoder"
                    )
                raise UnsupportedJPEGError(
                    f"unsupported JPEG frame marker FF{marker:02X}"
                )
            elif marker == 0xDD:
                if len(payload) != 2:
                    raise JPEGError("DRI payload shall be 2 bytes")
                self.restart_interval = int.from_bytes(payload, "big")
            elif marker == 0xE0 and payload.startswith(b"JFIF\x00"):
                self.jfif = True
            elif marker == 0xEE and payload.startswith(b"Adobe") and len(payload) >= 12:
                self.adobe_transform = payload[11]
            elif marker == 0xDA:
                if not frame_seen:
                    raise JPEGError("SOS occurs before SOF0")
                if scan_seen:
                    raise UnsupportedJPEGError(
                        "multiple-scan baseline JPEG requires the owned multi-scan decoder"
                    )
                self._parse_sos(payload)
                entropy_end = self._find_entropy_end(position)
                self.entropy = self.data[position:entropy_end]
                scan_seen = True
                position = entropy_end

        if not scan_seen:
            raise JPEGError("JPEG ended without SOS")

    def _find_entropy_end(self, start: int) -> int:
        position = start
        while position + 1 < len(self.data):
            if self.data[position] != 0xFF:
                position += 1
                continue
            probe = position + 1
            while probe < len(self.data) and self.data[probe] == 0xFF:
                probe += 1
            if probe >= len(self.data):
                return len(self.data)
            marker = self.data[probe]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                position = probe + 1
                continue
            return position
        return len(self.data)

    def _parse_dqt(self, payload: bytes) -> None:
        position = 0
        while position < len(payload):
            info = payload[position]
            position += 1
            precision, table_id = info >> 4, info & 0x0F
            if precision not in (0, 1) or table_id > 3:
                raise JPEGError("invalid DQT selector")
            width = 1 if precision == 0 else 2
            if position + 64 * width > len(payload):
                raise JPEGError("truncated DQT")
            zigzag: list[int] = []
            for _ in range(64):
                if width == 1:
                    value = payload[position]
                else:
                    value = int.from_bytes(payload[position : position + 2], "big")
                position += width
                if value == 0:
                    raise JPEGError("zero quantization value")
                zigzag.append(value)
            natural = [0] * 64
            for index, natural_index in enumerate(_ZIGZAG):
                natural[natural_index] = zigzag[index]
            self.quantization[table_id] = natural

    def _parse_dht(self, payload: bytes) -> None:
        position = 0
        while position < len(payload):
            info = payload[position]
            position += 1
            table_class, table_id = info >> 4, info & 0x0F
            if table_class not in (0, 1) or table_id > 3:
                raise JPEGError("invalid DHT selector")
            if position + 16 > len(payload):
                raise JPEGError("truncated DHT code lengths")
            counts = payload[position : position + 16]
            position += 16
            count = sum(counts)
            if position + count > len(payload):
                raise JPEGError("truncated DHT symbols")
            table = _Huffman(counts, payload[position : position + count])
            position += count
            (self.dc_tables if table_class == 0 else self.ac_tables)[table_id] = table

    def _parse_sof0(self, payload: bytes) -> None:
        if len(payload) < 6:
            raise JPEGError("truncated SOF0")
        self.precision = payload[0]
        self.height = int.from_bytes(payload[1:3], "big")
        self.width = int.from_bytes(payload[3:5], "big")
        count = payload[5]
        if self.width <= 0 or self.height <= 0 or count not in (1, 3, 4):
            raise JPEGError("invalid JPEG dimensions/component count")
        if len(payload) != 6 + 3 * count:
            raise JPEGError("SOF0 component list length mismatch")
        for index in range(count):
            base = 6 + 3 * index
            identifier = payload[base]
            sample = payload[base + 1]
            h, v = sample >> 4, sample & 0x0F
            quantization = payload[base + 2]
            if identifier in self.components or not (1 <= h <= 4 and 1 <= v <= 4):
                raise JPEGError("invalid SOF0 component/sampling factor")
            self.components[identifier] = _Component(
                identifier, h, v, quantization
            )

    def _parse_sos(self, payload: bytes) -> None:
        if not payload:
            raise JPEGError("truncated SOS")
        count = payload[0]
        if len(payload) != 1 + 2 * count + 3:
            raise JPEGError("SOS component list length mismatch")
        selected: list[_Component] = []
        for index in range(count):
            base = 1 + 2 * index
            identifier = payload[base]
            try:
                component = self.components[identifier]
            except KeyError as exc:
                raise JPEGError("SOS references unknown component") from exc
            selector = payload[base + 1]
            component.dc_table = selector >> 4
            component.ac_table = selector & 0x0F
            selected.append(component)
        if tuple(payload[-3:]) != (0, 63, 0):
            raise UnsupportedJPEGError(
                "baseline decoder requires Ss=0, Se=63 and Ah/Al=0"
            )
        if len(selected) != len(self.components):
            raise UnsupportedJPEGError(
                "baseline JPEG with components split across scans is not supported yet"
            )
        self.scan_components = selected

    @staticmethod
    def _extend(value: int, size: int) -> int:
        if size == 0:
            return 0
        threshold = 1 << (size - 1)
        return value if value >= threshold else value - ((1 << size) - 1)

    def _block(self, reader: _BitReader, component: _Component) -> list[int]:
        try:
            dc_table = self.dc_tables[component.dc_table]
            ac_table = self.ac_tables[component.ac_table]
            quant = self.quantization[component.quantization]
        except KeyError as exc:
            raise JPEGError("scan references missing DHT/DQT") from exc
        coefficients = [0] * 64
        category = dc_table.decode(reader)
        if category > 11:
            raise JPEGError("baseline DC category exceeds 11")
        difference = self._extend(reader.read(category), category)
        component.predictor += difference
        coefficients[0] = component.predictor * quant[0]
        zigzag = 1
        while zigzag < 64:
            symbol = ac_table.decode(reader)
            run, size = symbol >> 4, symbol & 0x0F
            if size == 0:
                if run == 0:
                    break
                if run == 15:
                    zigzag += 16
                    if zigzag > 64:
                        raise JPEGError("AC ZRL exceeds block")
                    continue
                raise JPEGError("invalid baseline AC symbol")
            zigzag += run
            if zigzag >= 64:
                raise JPEGError("AC run exceeds block")
            raw = self._extend(reader.read(size), size)
            natural = _ZIGZAG[zigzag]
            coefficients[natural] = raw * quant[natural]
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
                    row = v * 8
                    for u in range(8):
                        value = coefficients[row + u]
                        if value:
                            cu = _INV_SQRT2 if u == 0 else 1.0
                            total += (
                                cu * cv * value * _COS[x][u] * _COS[y][v]
                            )
                output[y * 8 + x] = max(
                    0, min(255, round(total / 4.0 + 128.0))
                )
        return output

    def _decode_scan(self) -> None:
        maximum_h = max(component.h for component in self.components.values())
        maximum_v = max(component.v for component in self.components.values())
        mcus_x = (self.width + 8 * maximum_h - 1) // (8 * maximum_h)
        mcus_y = (self.height + 8 * maximum_v - 1) // (8 * maximum_v)
        for component in self.components.values():
            component.plane_width = mcus_x * component.h * 8
            component.plane_height = mcus_y * component.v * 8
            component.plane = [0] * (
                component.plane_width * component.plane_height
            )
            component.predictor = 0

        reader = _BitReader(self.entropy)
        mcu_number = 0
        restart_number = 0
        for mcu_y in range(mcus_y):
            for mcu_x in range(mcus_x):
                if (
                    self.restart_interval
                    and mcu_number
                    and mcu_number % self.restart_interval == 0
                ):
                    reader.consume_restart(0xD0 + restart_number)
                    restart_number = (restart_number + 1) & 7
                    for component in self.components.values():
                        component.predictor = 0
                for component in self.scan_components:
                    assert component.plane is not None
                    for vertical in range(component.v):
                        for horizontal in range(component.h):
                            samples = self._idct(self._block(reader, component))
                            block_x = (mcu_x * component.h + horizontal) * 8
                            block_y = (mcu_y * component.v + vertical) * 8
                            for row in range(8):
                                start = (
                                    (block_y + row) * component.plane_width
                                    + block_x
                                )
                                component.plane[start : start + 8] = samples[
                                    row * 8 : row * 8 + 8
                                ]
                mcu_number += 1

    def _sample(
        self,
        component: _Component,
        x: int,
        y: int,
        maximum_h: int,
        maximum_v: int,
    ) -> int:
        assert component.plane is not None
        source_x = min(
            component.plane_width - 1, x * component.h // maximum_h
        )
        source_y = min(
            component.plane_height - 1, y * component.v // maximum_v
        )
        return component.plane[
            source_y * component.plane_width + source_x
        ]

    @staticmethod
    def _ycbcr(y: int, cb: int, cr: int) -> tuple[int, int, int]:
        cb_value = cb - 128.0
        cr_value = cr - 128.0
        values = (
            y + 1.402 * cr_value,
            y - 0.344136 * cb_value - 0.714136 * cr_value,
            y + 1.772 * cb_value,
        )
        return tuple(
            max(0, min(255, round(value))) for value in values
        )  # type: ignore[return-value]

    def _compose(self) -> JPEGImage:
        components = list(self.components.values())
        maximum_h = max(component.h for component in components)
        maximum_v = max(component.v for component in components)
        if len(components) == 1:
            output = bytearray(self.width * self.height)
            component = components[0]
            for y in range(self.height):
                for x in range(self.width):
                    output[y * self.width + x] = self._sample(
                        component, x, y, maximum_h, maximum_v
                    )
            return JPEGImage(self.width, self.height, "L", bytes(output))

        if len(components) == 3:
            output = bytearray(self.width * self.height * 3)
            target = 0
            direct_rgb = self.adobe_transform == 0 and not self.jfif
            for y in range(self.height):
                for x in range(self.width):
                    samples = tuple(
                        self._sample(component, x, y, maximum_h, maximum_v)
                        for component in components
                    )
                    rgb = samples if direct_rgb else self._ycbcr(*samples)
                    output[target : target + 3] = bytes(rgb)
                    target += 3
            return JPEGImage(self.width, self.height, "RGB", bytes(output))

        if len(components) == 4:
            output = bytearray(self.width * self.height * 4)
            target = 0
            for y in range(self.height):
                for x in range(self.width):
                    samples = tuple(
                        self._sample(component, x, y, maximum_h, maximum_v)
                        for component in components
                    )
                    if self.adobe_transform == 2:
                        r, g, b = self._ycbcr(samples[0], samples[1], samples[2])
                        cmyk = (255 - r, 255 - g, 255 - b, samples[3])
                    elif self.adobe_transform == 0:
                        cmyk = tuple(255 - sample for sample in samples)
                    else:
                        cmyk = samples
                    output[target : target + 4] = bytes(cmyk)
                    target += 4
            return JPEGImage(self.width, self.height, "CMYK", bytes(output))

        raise UnsupportedJPEGError(
            f"unsupported JPEG component count {len(components)}"
        )


def decode_jpeg(data: bytes) -> JPEGImage:
    return JPEGDecoder(data).decode()
