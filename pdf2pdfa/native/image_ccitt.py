"""CCITTFaxDecode adapter for owned PDF Image XObjects.

The bit-level fax codec in :mod:`ccitt` is kept independent of PDF object
syntax. This adapter applies PDF DecodeParms, accepts optional EOL markers even
when EndOfLine is false, uses the Image XObject height when Rows is zero, and
then feeds packed one-bit samples back through the normal owned image/color/
mask pipeline.
"""

from __future__ import annotations

from decimal import Decimal

from .ccitt import (
    CCITTError,
    UnsupportedCCITTError,
    _BitReader,
    _decode_1d_row,
    _decode_2d_row,
    _pack,
)
from .document import PDFDocument
from .filters import TERMINAL_IMAGE_FILTERS, decode_pipeline
from .image import (
    DecodedImage,
    ImageError,
    UnsupportedImageError,
    _filter_pipeline,
    _integer,
    _name,
    _stream,
    decode_image,
)
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .raster import Color
from .structure import resolve


def _exact_integer(doc: PDFDocument, value: PDFObject | None, default: int, label: str) -> int:
    if value is None:
        return default
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ImageError(f"CCITT {label} shall be an integer")
    integer = int(value)
    if integer != value:
        raise ImageError(f"CCITT {label} shall be an integer")
    return integer


def _boolean(doc: PDFDocument, value: PDFObject | None, default: bool, label: str) -> bool:
    if value is None:
        return default
    value = resolve(doc, value)
    if not isinstance(value, bool):
        raise ImageError(f"CCITT {label} shall be boolean")
    return value


def _consume_optional_eol(reader: _BitReader, *, required: bool) -> bool:
    start = reader.position
    zeros = 0
    try:
        while zeros <= 64:
            bit = reader.read_bit()
            if bit:
                if zeros >= 11:
                    return True
                reader.position = start
                if required:
                    raise CCITTError("required CCITT EOL marker is absent")
                return False
            zeros += 1
    except CCITTError:
        reader.position = start
        if required:
            raise
        return False
    reader.position = start
    if required:
        raise CCITTError("required CCITT EOL marker is absent")
    return False


def _decode_pdf_fax(
    data: bytes,
    *,
    columns: int,
    rows: int,
    k: int,
    end_of_line: bool,
    encoded_byte_align: bool,
    black_is_1: bool,
    damaged_rows_before_error: int,
) -> bytes:
    if columns <= 0 or rows <= 0 or columns * rows > 250_000_000:
        raise CCITTError(f"invalid/unsafe CCITT dimensions {columns}x{rows}")
    if k > 0:
        raise UnsupportedCCITTError(
            "mixed Group 3 CCITT (K > 0) is not implemented by the owned decoder"
        )
    if damaged_rows_before_error:
        raise UnsupportedCCITTError(
            "DamagedRowsBeforeError recovery is not implemented by the owned decoder"
        )

    reader = _BitReader(data)
    decoded: list[list[bool]] = []
    reference = [False] * columns
    for row_number in range(rows):
        # EncodedByteAlign means extra zero bits may precede each encoded line
        # so its first significant code begins at a byte boundary.
        if encoded_byte_align:
            reader.align_byte()
        _consume_optional_eol(reader, required=end_of_line)
        try:
            if k == 0:
                row = _decode_1d_row(reader, columns)
            else:
                row = _decode_2d_row(reader, reference, columns)
        except CCITTError as exc:
            raise CCITTError(f"CCITT row {row_number + 1}: {exc}") from exc
        decoded.append(row)
        reference = row
    return _pack(decoded, black_is_1)


def _ccitt_payload(
    doc: PDFDocument,
    stream: PDFStream,
    *,
    width: int,
    height: int,
) -> bytes | None:
    filters, parms = _filter_pipeline(doc, stream)
    positions = [
        index for index, name in enumerate(filters)
        if name in ("CCITTFaxDecode", "CCF")
    ]
    if not positions:
        return None
    if len(positions) != 1 or positions[0] != len(filters) - 1:
        raise UnsupportedImageError(
            "CCITTFaxDecode shall be the final fax stage in the owned image pipeline"
        )
    if any(
        name in TERMINAL_IMAGE_FILTERS
        for name in filters[: positions[0]]
    ):
        raise UnsupportedImageError(
            "another terminal image codec precedes CCITTFaxDecode"
        )

    encoded = decode_pipeline(
        stream.data,
        filters[: positions[0]],
        parms[: positions[0]],
    )
    params = parms[positions[0]] or PDFDict()
    k = _exact_integer(doc, params.get("K"), 0, "/K")
    columns = _exact_integer(doc, params.get("Columns"), 1728, "/Columns")
    rows_value = _exact_integer(doc, params.get("Rows"), 0, "/Rows")
    end_of_line = _boolean(doc, params.get("EndOfLine"), False, "/EndOfLine")
    encoded_byte_align = _boolean(
        doc, params.get("EncodedByteAlign"), False, "/EncodedByteAlign"
    )
    black_is_1 = _boolean(doc, params.get("BlackIs1"), False, "/BlackIs1")
    damaged = _exact_integer(
        doc,
        params.get("DamagedRowsBeforeError"),
        0,
        "/DamagedRowsBeforeError",
    )

    rows = height if rows_value == 0 else rows_value
    if columns != width:
        raise UnsupportedImageError(
            f"CCITT /Columns {columns} disagrees with Image /Width {width}; "
            "owned rendering refuses to crop/restride fax rows implicitly"
        )
    if rows != height:
        raise UnsupportedImageError(
            f"CCITT /Rows {rows} disagrees with Image /Height {height}"
        )
    try:
        return _decode_pdf_fax(
            encoded,
            columns=columns,
            rows=rows,
            k=k,
            end_of_line=end_of_line,
            encoded_byte_align=encoded_byte_align,
            black_is_1=black_is_1,
            damaged_rows_before_error=damaged,
        )
    except UnsupportedCCITTError as exc:
        raise UnsupportedImageError(str(exc)) from exc
    except CCITTError as exc:
        raise ImageError(str(exc)) from exc


def decode_image_owned(
    doc: PDFDocument,
    value: PDFObject,
    *,
    resources: PDFDict | None = None,
    stencil_color: Color = Color(0, 0, 0, 1),
    _depth: int = 0,
) -> DecodedImage:
    """Decode an image, adding owned CCITTFaxDecode to the base image stack."""
    stream = _stream(doc, value)
    if stream is None:
        raise ImageError("Image XObject is not a stream")
    width = _integer(doc, stream.get("Width"))
    height = _integer(doc, stream.get("Height"))
    if width <= 0 or height <= 0:
        raise ImageError(f"invalid image dimensions {width}x{height}")
    payload = _ccitt_payload(doc, stream, width=width, height=height)
    if payload is None:
        return decode_image(
            doc,
            value,
            resources=resources,
            stencil_color=stencil_color,
            _depth=_depth,
        )

    dictionary = PDFDict(
        {
            key: item
            for key, item in stream.dictionary.items()
            if key not in {"Filter", "DecodeParms", "DP"}
        }
    )
    decoded_stream = PDFStream(dictionary, payload)
    return decode_image(
        doc,
        decoded_stream,
        resources=resources,
        stencil_color=stencil_color,
        _depth=_depth,
    )
