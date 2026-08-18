"""Strict owned decoder for PDF Image XObjects.

Supports packed samples, generic stream filters, baseline DCT/JPEG, PDF color
spaces, Decode arrays, stencil masks, color-key masks, explicit masks and soft
masks. JPX/JBIG2/CCITT remain explicit unsupported codecs until their owned
decoders are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .color import ColorSpace, ColorSpaceError, parse_color_space
from .document import PDFDocument
from .filters import TERMINAL_IMAGE_FILTERS, decode_pipeline
from .jpeg import JPEGImage, decode_jpeg
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .raster import Color
from .structure import resolve


class ImageError(ValueError):
    pass


class UnsupportedImageError(ImageError):
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


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    try:
        value = resolve(doc, value)
    except Exception:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return default
    return float(value)


def _integer(doc: PDFDocument, value: PDFObject | None, default: int = 0) -> int:
    return int(_number(doc, value, float(default)))


def _name(doc: PDFDocument, value: PDFObject | None) -> str:
    try:
        value = resolve(doc, value)
    except Exception:
        return ""
    return value.value if isinstance(value, PDFName) else ""


def _stream(doc: PDFDocument, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _filter_pipeline(
    doc: PDFDocument, stream: PDFStream
) -> tuple[list[str], list[PDFDict | None]]:
    raw_filter = resolve(doc, stream.get("Filter")) if stream.get("Filter") is not None else None
    if raw_filter is None:
        filters: list[str] = []
    elif isinstance(raw_filter, PDFName):
        filters = [raw_filter.value]
    elif isinstance(raw_filter, list):
        filters = []
        for item in raw_filter:
            item = resolve(doc, item)
            if not isinstance(item, PDFName):
                raise ImageError("Image /Filter array contains a non-name")
            filters.append(item.value)
    else:
        raise ImageError("Image /Filter is not a name or array")

    raw_parms = resolve(doc, stream.get("DecodeParms")) if stream.get("DecodeParms") is not None else None
    if raw_parms is None:
        parms = [None] * len(filters)
    elif isinstance(raw_parms, PDFDict):
        parms = [raw_parms] + [None] * max(0, len(filters) - 1)
    elif isinstance(raw_parms, list):
        parms = []
        for item in raw_parms:
            item = resolve(doc, item)
            parms.append(item if isinstance(item, PDFDict) else None)
        parms.extend([None] * max(0, len(filters) - len(parms)))
        parms = parms[: len(filters)]
    else:
        raise ImageError("Image /DecodeParms is malformed")
    return filters, parms


def _payload(doc: PDFDocument, stream: PDFStream) -> bytes | JPEGImage:
    filters, parms = _filter_pipeline(doc, stream)
    terminal = [i for i, name in enumerate(filters) if name in TERMINAL_IMAGE_FILTERS]
    if not terminal:
        return decode_pipeline(stream.data, filters, parms)
    if len(terminal) != 1 or terminal[0] != len(filters) - 1:
        raise UnsupportedImageError(
            "terminal image codec shall be the final and only terminal filter"
        )
    position = terminal[0]
    encoded = decode_pipeline(stream.data, filters[:position], parms[:position])
    name = filters[position]
    if name in ("DCTDecode", "DCT"):
        return decode_jpeg(encoded)
    if name == "JPXDecode":
        raise UnsupportedImageError("JPXDecode requires the owned JPEG 2000 decoder")
    if name == "JBIG2Decode":
        raise UnsupportedImageError("JBIG2Decode requires the owned JBIG2 decoder")
    if name in ("CCITTFaxDecode", "CCF"):
        raise UnsupportedImageError("CCITTFaxDecode requires the owned fax decoder")
    raise UnsupportedImageError(f"unsupported terminal image filter /{name}")


def _samples(
    data: bytes,
    *,
    width: int,
    height: int,
    components: int,
    bits: int,
) -> list[int]:
    if bits not in (1, 2, 4, 8, 16):
        raise UnsupportedImageError(f"unsupported image BitsPerComponent {bits}")
    row_bits = width * components * bits
    row_bytes = (row_bits + 7) // 8
    if len(data) < row_bytes * height:
        raise ImageError("decoded image payload is truncated")
    result: list[int] = []
    mask = (1 << bits) - 1
    for row in range(height):
        chunk = data[row * row_bytes : (row + 1) * row_bytes]
        if bits == 8:
            result.extend(chunk[: width * components])
            continue
        if bits == 16:
            for offset in range(0, width * components * 2, 2):
                result.append(int.from_bytes(chunk[offset : offset + 2], "big"))
            continue
        bit_position = 0
        for _ in range(width * components):
            byte_index = bit_position // 8
            shift = 8 - bits - (bit_position % 8)
            result.append((chunk[byte_index] >> shift) & mask)
            bit_position += bits
    return result


def _decode_pairs(
    doc: PDFDocument,
    value: PDFObject | None,
    *,
    components: int,
    indexed: bool,
    maximum: int,
) -> tuple[tuple[float, float], ...]:
    if value is None:
        # Indexed samples are indices by default: decode [0, 2^bpc-1], then
        # the color-space converter clamps to hival. Other spaces normalize.
        if indexed:
            return ((0.0, float(maximum)),)
        return tuple((0.0, 1.0) for _ in range(components))
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != 2 * components:
        raise ImageError("Image /Decode length does not match components")
    return tuple(
        (_number(doc, value[2 * i]), _number(doc, value[2 * i + 1]))
        for i in range(components)
    )


def _mapped(raw: int, maximum: int, pair: tuple[float, float]) -> float:
    if maximum <= 0:
        return pair[0]
    return pair[0] + raw / maximum * (pair[1] - pair[0])


def _jpeg_samples(
    image: JPEGImage,
    width: int,
    height: int,
) -> tuple[list[int], int]:
    if (image.width, image.height) != (width, height):
        raise ImageError(
            f"JPEG dimensions {image.width}x{image.height} disagree with "
            f"Image XObject {width}x{height}"
        )
    return list(image.pixels), image.components


def _nearest_alpha(image: DecodedImage, width: int, height: int) -> list[int]:
    result: list[int] = []
    for y in range(height):
        sy = min(image.height - 1, y * image.height // height)
        for x in range(width):
            sx = min(image.width - 1, x * image.width // width)
            result.append(image.rgba[(sy * image.width + sx) * 4 + 3])
    return result


def _nearest_luminosity(image: DecodedImage, width: int, height: int) -> list[int]:
    result: list[int] = []
    for y in range(height):
        sy = min(image.height - 1, y * image.height // height)
        for x in range(width):
            sx = min(image.width - 1, x * image.width // width)
            offset = (sy * image.width + sx) * 4
            r, g, b = image.rgba[offset : offset + 3]
            result.append(round(0.299 * r + 0.587 * g + 0.114 * b))
    return result


def _matte_rgb(
    doc: PDFDocument,
    smask: PDFStream,
    color_space: ColorSpace,
) -> tuple[float, float, float] | None:
    raw = resolve(doc, smask.get("Matte")) if smask.get("Matte") is not None else None
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != color_space.components:
        raise ImageError("soft-mask /Matte length does not match source ColorSpace")
    values = tuple(_number(doc, item) for item in raw)
    return color_space.rgb(values)


def decode_image(
    doc: PDFDocument,
    value: PDFObject,
    *,
    resources: PDFDict | None = None,
    stencil_color: Color = Color(0, 0, 0, 1),
    _depth: int = 0,
) -> DecodedImage:
    if _depth > 8:
        raise ImageError("image-mask recursion exceeds 8 levels")
    stream = _stream(doc, value)
    if stream is None:
        raise ImageError("Image XObject is not a stream")
    width = _integer(doc, stream.get("Width"))
    height = _integer(doc, stream.get("Height"))
    if width <= 0 or height <= 0 or width * height > 250_000_000:
        raise ImageError(f"invalid/unsafe image dimensions {width}x{height}")
    interpolate = bool(resolve(doc, stream.get("Interpolate"))) if stream.get("Interpolate") is not None else False
    image_mask = bool(resolve(doc, stream.get("ImageMask"))) if stream.get("ImageMask") is not None else False
    bits = 1 if image_mask else _integer(doc, stream.get("BitsPerComponent"))
    if bits not in (1, 2, 4, 8, 16):
        raise UnsupportedImageError(f"unsupported BitsPerComponent {bits}")
    maximum = (1 << bits) - 1

    decoded = _payload(doc, stream)
    color_space: ColorSpace | None = None
    if image_mask:
        components = 1
        if isinstance(decoded, JPEGImage):
            raise ImageError("ImageMask cannot use DCT JPEG")
        raw_samples = _samples(
            decoded, width=width, height=height, components=1, bits=1
        )
    else:
        try:
            color_space = parse_color_space(
                doc, stream.get("ColorSpace"), resources=resources
            )
        except ColorSpaceError as exc:
            raise ImageError(str(exc)) from exc
        components = color_space.components
        if isinstance(decoded, JPEGImage):
            raw_samples, jpeg_components = _jpeg_samples(decoded, width, height)
            if jpeg_components != components:
                raise ImageError(
                    "JPEG component count disagrees with PDF ColorSpace"
                )
            # JPEG decoder emits 8-bit component samples.
            maximum = 255
            bits = 8
        else:
            raw_samples = _samples(
                decoded,
                width=width,
                height=height,
                components=components,
                bits=bits,
            )

    pairs = _decode_pairs(
        doc,
        stream.get("Decode"),
        components=components,
        indexed=bool(color_space and color_space.is_indexed),
        maximum=maximum,
    )

    # Color-key masks compare encoded samples before Decode mapping.
    key_mask: list[tuple[int, int]] | None = None
    explicit_mask: DecodedImage | None = None
    raw_mask = resolve(doc, stream.get("Mask")) if stream.get("Mask") is not None else None
    if isinstance(raw_mask, list):
        if len(raw_mask) != 2 * components:
            raise ImageError("color-key /Mask length does not match components")
        key_mask = [
            (
                _integer(doc, raw_mask[2 * i]),
                _integer(doc, raw_mask[2 * i + 1]),
            )
            for i in range(components)
        ]
    elif raw_mask is not None:
        if _stream(doc, raw_mask) is None:
            raise ImageError("Image /Mask is neither array nor image stream")
        explicit_mask = decode_image(
            doc,
            raw_mask,
            resources=resources,
            stencil_color=Color(1, 1, 1, 1),
            _depth=_depth + 1,
        )

    soft_mask: DecodedImage | None = None
    soft_mask_stream: PDFStream | None = None
    raw_smask = stream.get("SMask")
    if raw_smask is not None and _name(doc, raw_smask) != "None":
        soft_mask_stream = _stream(doc, raw_smask)
        if soft_mask_stream is None:
            raise ImageError("Image /SMask is not an image stream")
        soft_mask = decode_image(
            doc,
            raw_smask,
            resources=resources,
            stencil_color=Color(1, 1, 1, 1),
            _depth=_depth + 1,
        )

    explicit_alpha = (
        _nearest_alpha(explicit_mask, width, height) if explicit_mask else None
    )
    soft_alpha = (
        _nearest_luminosity(soft_mask, width, height) if soft_mask else None
    )
    matte = (
        _matte_rgb(doc, soft_mask_stream, color_space)
        if soft_mask_stream is not None and color_space is not None
        else None
    )

    output = bytearray(width * height * 4)
    for pixel in range(width * height):
        base = pixel * components
        sample_values = raw_samples[base : base + components]
        if image_mask:
            decoded_mask = _mapped(sample_values[0], maximum, pairs[0])
            # For a stencil mask, decoded 0 paints and decoded 1 leaves the
            # backdrop unchanged. Reversed Decode naturally reverses this.
            alpha = 255 if decoded_mask < 0.5 else 0
            rgb = (stencil_color.r, stencil_color.g, stencil_color.b)
            alpha = round(alpha * stencil_color.a)
        else:
            assert color_space is not None
            mapped_values = tuple(
                _mapped(sample_values[index], maximum, pairs[index])
                for index in range(components)
            )
            rgb = color_space.rgb(mapped_values)
            alpha = 255
            if key_mask is not None and all(
                low <= sample_values[index] <= high
                for index, (low, high) in enumerate(key_mask)
            ):
                alpha = 0

        if explicit_alpha is not None:
            alpha = round(alpha * explicit_alpha[pixel] / 255.0)
        if soft_alpha is not None:
            alpha = round(alpha * soft_alpha[pixel] / 255.0)

        # /Matte means source color samples were premultiplied against Matte
        # before application of the soft mask. Undo that compositing before
        # placing the image on an arbitrary backdrop.
        if matte is not None and alpha > 0:
            a = alpha / 255.0
            rgb = tuple(
                _clamp_channel((rgb[channel] - (1.0 - a) * matte[channel]) / a)
                for channel in range(3)
            )

        offset = pixel * 4
        output[offset : offset + 4] = bytes(
            [
                round(_clamp_channel(rgb[0]) * 255),
                round(_clamp_channel(rgb[1]) * 255),
                round(_clamp_channel(rgb[2]) * 255),
                max(0, min(255, alpha)),
            ]
        )
    return DecodedImage(width, height, bytes(output), interpolate)


def _clamp_channel(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
