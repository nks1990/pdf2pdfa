"""Owned decoding of PDF Image XObjects and stencil/soft masks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math

from .document import PDFDocument
from .filters import TERMINAL_IMAGE_FILTERS, decode_pipeline
from .icc import parse_icc
from .icc_transform import ICCDeviceToRGB, ICCTransformError
from .jpeg import JPEGImage, UnsupportedJPEGError, decode_jpeg
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .raster import Color
from .structure import resolve


class PDFImageError(ValueError):
    pass


class UnsupportedPDFImageError(PDFImageError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedImage:
    width: int
    height: int
    rgba: bytes
    interpolate: bool = False

    def pixel(self, x: int, y: int) -> Color:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError((x, y))
        offset = (y * self.width + x) * 4
        return Color(*(self.rgba[offset + i] / 255.0 for i in range(4)))


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _stream(doc: PDFDocument, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    try:
        value = resolve(doc, value)
    except Exception:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, Decimal)):
        return float(value)
    return default


def _integer(doc: PDFDocument, value: PDFObject | None, default: int = 0) -> int:
    return int(_number(doc, value, default))


def _filter_pipeline(doc: PDFDocument, stream: PDFStream) -> tuple[list[str], list[PDFDict | None]]:
    value = resolve(doc, stream.get("Filter")) if stream.get("Filter") is not None else None
    if value is None:
        filters: list[str] = []
    elif isinstance(value, PDFName):
        filters = [value.value]
    elif isinstance(value, list):
        filters = []
        for item in value:
            item = resolve(doc, item)
            if not isinstance(item, PDFName):
                raise PDFImageError("Image /Filter array contains a non-name")
            filters.append(item.value)
    else:
        raise PDFImageError("Image /Filter is not a name or array")
    parms_value = resolve(doc, stream.get("DecodeParms")) if stream.get("DecodeParms") is not None else None
    if parms_value is None:
        parms = [None] * len(filters)
    elif isinstance(parms_value, PDFDict):
        parms = [parms_value] + [None] * max(0, len(filters) - 1)
    elif isinstance(parms_value, list):
        parms = []
        for item in parms_value:
            item = resolve(doc, item)
            parms.append(item if isinstance(item, PDFDict) else None)
        parms.extend([None] * max(0, len(filters) - len(parms)))
        parms = parms[: len(filters)]
    else:
        parms = [None] * len(filters)
    return filters, parms


def _decoded_payload(doc: PDFDocument, stream: PDFStream) -> tuple[bytes | JPEGImage, str | None]:
    filters, parms = _filter_pipeline(doc, stream)
    terminal_positions = [index for index, name in enumerate(filters) if name in TERMINAL_IMAGE_FILTERS]
    if not terminal_positions:
        return decode_pipeline(stream.data, filters, parms), None
    if len(terminal_positions) != 1 or terminal_positions[0] != len(filters) - 1:
        raise UnsupportedPDFImageError(
            "owned image decoder requires terminal image codec to be the final and only terminal filter"
        )
    terminal_index = terminal_positions[0]
    prefix = decode_pipeline(stream.data, filters[:terminal_index], parms[:terminal_index])
    terminal = filters[terminal_index]
    if terminal in ("DCTDecode", "DCT"):
        return decode_jpeg(prefix), "DCTDecode"
    if terminal == "JPXDecode":
        raise UnsupportedPDFImageError("JPEG 2000 / JPXDecode requires owned JPX decoder")
    if terminal == "JBIG2Decode":
        raise UnsupportedPDFImageError("JBIG2Decode requires owned JBIG2 decoder")
    if terminal in ("CCITTFaxDecode", "CCF"):
        raise UnsupportedPDFImageError("CCITT Fax requires owned fax decoder")
    raise UnsupportedPDFImageError(f"unsupported terminal image filter /{terminal}")


def _unpack_samples(data: bytes, *, width: int, height: int, components: int, bpc: int) -> list[int]:
    if bpc not in (1, 2, 4, 8, 16):
        raise UnsupportedPDFImageError(f"unsupported image BitsPerComponent {bpc}")
    if width <= 0 or height <= 0 or components <= 0:
        raise PDFImageError("invalid image dimensions/component count")
    row_bits = width * components * bpc
    row_bytes = (row_bits + 7) // 8
    if len(data) < row_bytes * height:
        raise PDFImageError(
            f"decoded image payload is too short: {len(data)} < {row_bytes * height}"
        )
    output: list[int] = []
    mask = (1 << bpc) - 1
    for row in range(height):
        chunk = data[row * row_bytes : (row + 1) * row_bytes]
        if bpc == 8:
            output.extend(chunk[: width * components])
            continue
        if bpc == 16:
            needed = width * components * 2
            for position in range(0, needed, 2):
                output.append(int.from_bytes(chunk[position : position + 2], "big"))
            continue
        bit_position = 0
        for _ in range(width * components):
            byte_index = bit_position // 8
            shift = 8 - bpc - (bit_position % 8)
            output.append((chunk[byte_index] >> shift) & mask)
            bit_position += bpc
    return output


def _decode_array(doc: PDFDocument, value: PDFObject | None, components: int) -> tuple[tuple[float, float], ...]:
    if value is None:
        return tuple((0.0, 1.0) for _ in range(components))
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != 2 * components:
        raise PDFImageError("Image /Decode array length does not match color components")
    pairs = []
    for index in range(components):
        pairs.append(
            (
                _number(doc, value[index * 2], 0.0),
                _number(doc, value[index * 2 + 1], 1.0),
            )
        )
    return tuple(pairs)


def _map_sample(raw: int, maximum: int, decode: tuple[float, float]) -> float:
    if maximum <= 0:
        return decode[0]
    return decode[0] + (raw / maximum) * (decode[1] - decode[0])


@dataclass(frozen=True, slots=True)
class _ColorSpace:
    components: int
    convert: object

    def rgb(self, values: tuple[float, ...]) -> tuple[float, float, float]:
        return self.convert(values)  # type: ignore[operator,no-any-return]


def _device_rgb(values: tuple[float, ...]) -> tuple[float, float, float]:
    return tuple(max(0.0, min(1.0, value)) for value in values[:3])  # type: ignore[return-value]


def _device_gray(values: tuple[float, ...]) -> tuple[float, float, float]:
    value = max(0.0, min(1.0, values[0]))
    return (value, value, value)


def _device_cmyk(values: tuple[float, ...]) -> tuple[float, float, float]:
    c, m, y, k = (max(0.0, min(1.0, value)) for value in values[:4])
    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


def _lab(values: tuple[float, ...], white: tuple[float, float, float], range_ab) -> tuple[float, float, float]:
    lstar = max(0.0, min(100.0, values[0]))
    astar = max(range_ab[0], min(range_ab[1], values[1]))
    bstar = max(range_ab[2], min(range_ab[3], values[2]))
    fy = (lstar + 16.0) / 116.0
    fx = fy + astar / 500.0
    fz = fy - bstar / 200.0
    delta = 6.0 / 29.0

    def inverse(value: float) -> float:
        return value ** 3 if value > delta else 3 * delta * delta * (value - 4 / 29)

    x, y, z = white[0] * inverse(fx), white[1] * inverse(fy), white[2] * inverse(fz)
    # D50-adapted sRGB inverse matrix.
    linear = (
        3.1338561 * x - 1.6168667 * y - 0.4906146 * z,
        -0.9787684 * x + 1.9161415 * y + 0.0334540 * z,
        0.0719453 * x - 0.2289914 * y + 1.4052427 * z,
    )

    def encode(value: float) -> float:
        value = max(0.0, value)
        return max(0.0, min(1.0, 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055))

    return tuple(encode(value) for value in linear)  # type: ignore[return-value]


def _color_space(doc: PDFDocument, value: PDFObject | None) -> _ColorSpace:
    value = resolve(doc, value)
    if isinstance(value, PDFName):
        if value.value in ("DeviceGray", "G"):
            return _ColorSpace(1, _device_gray)
        if value.value in ("DeviceRGB", "RGB"):
            return _ColorSpace(3, _device_rgb)
        if value.value in ("DeviceCMYK", "CMYK"):
            return _ColorSpace(4, _device_cmyk)
        raise UnsupportedPDFImageError(f"unresolved/unsupported image color space /{value.value}")
    if not isinstance(value, list) or not value:
        raise PDFImageError("image ColorSpace is malformed")
    family = _name(resolve(doc, value[0]))
    if family == "ICCBased" and len(value) >= 2:
        profile_stream = _stream(doc, value[1])
        if profile_stream is None:
            raise PDFImageError("ICCBased color space lacks ICC stream")
        profile_bytes, terminal = _decoded_payload(doc, profile_stream)
        if not isinstance(profile_bytes, bytes) or terminal is not None:
            raise PDFImageError("ICC profile stream cannot use image terminal codec")
        profile = parse_icc(profile_bytes)
        transform = ICCDeviceToRGB(profile)
        return _ColorSpace(profile.components, transform)
    if family == "Indexed" and len(value) >= 4:
        base = _color_space(doc, value[1])
        hival = _integer(doc, value[2], -1)
        lookup = resolve(doc, value[3])
        if isinstance(lookup, PDFStream):
            payload, terminal = _decoded_payload(doc, lookup)
            if not isinstance(payload, bytes) or terminal is not None:
                raise PDFImageError("Indexed lookup stream has invalid codec")
            lookup_bytes = payload
        elif isinstance(lookup, bytes):
            lookup_bytes = lookup
        else:
            raise PDFImageError("Indexed lookup is not string/stream")
        expected = (hival + 1) * base.components
        if hival < 0 or len(lookup_bytes) < expected:
            raise PDFImageError("Indexed lookup table is truncated")

        def indexed(values: tuple[float, ...]) -> tuple[float, float, float]:
            index = max(0, min(hival, int(round(values[0]))))
            offset = index * base.components
            components = tuple(
                lookup_bytes[offset + component] / 255.0
                for component in range(base.components)
            )
            return base.rgb(components)

        return _ColorSpace(1, indexed)
    if family == "Lab" and len(value) >= 2:
        params = _dict(doc, value[1])
        if params is None:
            raise PDFImageError("Lab color space parameters are malformed")
        wp = resolve(doc, params.get("WhitePoint"))
        if not isinstance(wp, list) or len(wp) != 3:
            raise PDFImageError("Lab color space requires WhitePoint")
        white = tuple(_number(doc, component) for component in wp)
        range_value = resolve(doc, params.get("Range")) if params.get("Range") is not None else None
        if isinstance(range_value, list) and len(range_value) == 4:
            range_ab = tuple(_number(doc, component) for component in range_value)
        else:
            range_ab = (-100.0, 100.0, -100.0, 100.0)
        return _ColorSpace(3, lambda values: _lab(values, white, range_ab))
    if family in ("CalGray", "CalRGB"):
        # Calibration parameters are honored by a future full color-space module.
        # Using device formulas here would silently discard WhitePoint/Gamma, so fail.
        raise UnsupportedPDFImageError(
            f"/{family} image conversion requires owned calibrated-color transform"
        )
    if family in ("Separation", "DeviceN", "Pattern"):
        raise UnsupportedPDFImageError(
            f"/{family} image conversion requires owned tint/pattern function evaluator"
        )
    raise UnsupportedPDFImageError(f"unsupported image color-space family /{family}")


def _scale_mask(mask: DecodedImage, width: int, height: int) -> list[int]:
    result: list[int] = []
    for y in range(height):
        sy = min(mask.height - 1, (y * mask.height) // height)
        for x in range(width):
            sx = min(mask.width - 1, (x * mask.width) // width)
            offset = (sy * mask.width + sx) * 4
            r, g, b = mask.rgba[offset], mask.rgba[offset + 1], mask.rgba[offset + 2]
            result.append(round(0.299 * r + 0.587 * g + 0.114 * b))
    return result


def _from_jpeg(image: JPEGImage, *, width: int, height: int) -> tuple[list[int], int, int]:
    if image.width != width or image.height != height:
        raise PDFImageError(
            f"DCT JPEG dimensions {image.width}x{image.height} disagree with PDF {width}x{height}"
        )
    if image.mode == "L":
        return list(image.pixels), 1, 255
    if image.mode == "RGB":
        return list(image.pixels), 3, 255
    if image.mode == "CMYK":
        return list(image.pixels), 4, 255
    raise UnsupportedPDFImageError(f"unsupported decoded JPEG mode {image.mode}")


def decode_pdf_image(
    doc: PDFDocument,
    stream_value: PDFObject,
    *,
    stencil_color: Color = Color(0, 0, 0, 1),
    _depth: int = 0,
) -> DecodedImage:
    if _depth > 8:
        raise PDFImageError("image mask recursion is too deep")
    stream = _stream(doc, stream_value)
    if stream is None:
        raise PDFImageError("image object is not a stream")
    width = _integer(doc, stream.get("Width"), 0)
    height = _integer(doc, stream.get("Height"), 0)
    if width <= 0 or height <= 0 or width * height > 250_000_000:
        raise PDFImageError(f"invalid/unsafe image dimensions {width}x{height}")
    interpolate = bool(resolve(doc, stream.get("Interpolate"))) if stream.get("Interpolate") is not None else False
    image_mask = bool(resolve(doc, stream.get("ImageMask"))) if stream.get("ImageMask") is not None else False
    bpc = 1 if image_mask else _integer(doc, stream.get("BitsPerComponent"), 0)
    if bpc not in (1, 2, 4, 8, 16):
        raise UnsupportedPDFImageError(f"unsupported BitsPerComponent {bpc}")

    payload, terminal = _decoded_payload(doc, stream)
    raw_samples: list[int]
    maximum: int
    components: int
    color: _ColorSpace | None = None
    if isinstance(payload, JPEGImage):
        raw_samples, components, maximum = _from_jpeg(payload, width=width, height=height)
        if image_mask:
            raise PDFImageError("ImageMask cannot be DCT JPEG")
        if stream.get("ColorSpace") is None:
            color = _ColorSpace(
                components,
                _device_gray if components == 1 else _device_rgb if components == 3 else _device_cmyk,
            )
        else:
            color = _color_space(doc, stream.get("ColorSpace"))
            if color.components != components:
                raise PDFImageError("JPEG component count disagrees with PDF ColorSpace")
    else:
        if image_mask:
            components = 1
            raw_samples = _unpack_samples(payload, width=width, height=height, components=1, bpc=1)
            maximum = 1
        else:
            color = _color_space(doc, stream.get("ColorSpace"))
            components = color.components
            raw_samples = _unpack_samples(
                payload,
                width=width,
                height=height,
                components=components,
                bpc=bpc,
            )
            maximum = (1 << bpc) - 1

    decode = _decode_array(doc, stream.get("Decode"), components)
    color_key = resolve(doc, stream.get("Mask")) if stream.get("Mask") is not None else None
    color_key_ranges: list[tuple[int, int]] | None = None
    explicit_mask: DecodedImage | None = None
    if isinstance(color_key, list):
        if len(color_key) != components * 2:
            raise PDFImageError("color-key /Mask length does not match components")
        color_key_ranges = []
        for component in range(components):
            low = _integer(doc, color_key[component * 2], 0)
            high = _integer(doc, color_key[component * 2 + 1], maximum)
            color_key_ranges.append((low, high))
    elif color_key is not None:
        mask_stream = _stream(doc, color_key)
        if mask_stream is not None:
            explicit_mask = decode_pdf_image(
                doc,
                color_key,
                stencil_color=Color(1, 1, 1, 1),
                _depth=_depth + 1,
            )

    soft_mask_value = stream.get("SMask")
    soft_mask = None
    if soft_mask_value is not None and _name(resolve(doc, soft_mask_value)) != "None":
        soft_mask = decode_pdf_image(
            doc,
            soft_mask_value,
            stencil_color=Color(1, 1, 1, 1),
            _depth=_depth + 1,
        )
    alpha_soft = _scale_mask(soft_mask, width, height) if soft_mask else None
    alpha_explicit = _scale_mask(explicit_mask, width, height) if explicit_mask else None

    rgba = bytearray(width * height * 4)
    for pixel in range(width * height):
        base = pixel * components
        samples = raw_samples[base : base + components]
        if image_mask:
            mapped = _map_sample(samples[0], maximum, decode[0])
            # Stencil paints where decoded mask sample is zero according to PDF image-mask semantics.
            alpha = 255 if mapped < 0.5 else 0
            rgb = (stencil_color.r, stencil_color.g, stencil_color.b)
            alpha = round(alpha * stencil_color.a)
        else:
            assert color is not None
            mapped_values = tuple(
                _map_sample(samples[index], maximum, decode[index])
                for index in range(components)
            )
            # Indexed Decode maps sample to palette index rather than [0,1].
            if isinstance(resolve(doc, stream.get("ColorSpace")), list):
                cs_array = resolve(doc, stream.get("ColorSpace"))
                if isinstance(cs_array, list) and cs_array and _name(resolve(doc, cs_array[0])) == "Indexed":
                    hival = _integer(doc, cs_array[2], 0)
                    mapped_values = (
                        _map_sample(samples[0], maximum, decode[0]) * hival,
                    )
            rgb = color.rgb(mapped_values)
            alpha = 255
            if color_key_ranges and all(
                low <= samples[index] <= high
                for index, (low, high) in enumerate(color_key_ranges)
            ):
                alpha = 0
        if alpha_explicit is not None:
            alpha = round(alpha * (alpha_explicit[pixel] / 255.0))
        if alpha_soft is not None:
            alpha = round(alpha * (alpha_soft[pixel] / 255.0))
        offset = pixel * 4
        rgba[offset : offset + 4] = bytes(
            [
                round(max(0.0, min(1.0, rgb[0])) * 255),
                round(max(0.0, min(1.0, rgb[1])) * 255),
                round(max(0.0, min(1.0, rgb[2])) * 255),
                max(0, min(255, alpha)),
            ]
        )
    return DecodedImage(width, height, bytes(rgba), interpolate=interpolate)
