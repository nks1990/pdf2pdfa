"""PDF Type1C/CIDFontType0C adapter for the owned CFF1 engine.

This module maps PDF character codes/CIDs to the original embedded CFF
CharStrings. It does not convert CFF outlines to TrueType and does not replace
fonts. PDF Widths/DW/W/DW2/W2 remain authoritative for text placement; CFF
FontMatrix maps CharString coordinates into text space for painting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .cff import CFFError, CFFFont, UnsupportedCFFError
from .cmap import CMapError
from .cmap_registry import resolve_type0_cmap
from .document import PDFDocument
from .font_encoding import FontEncodingError, parse_encoding
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .raster import Matrix, Path
from .structure import decoded_stream_bytes, resolve
from .vertical_metrics import VerticalMetric, VerticalMetrics, VerticalMetricsError


class CFFPDFFontError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CFFGlyphItem:
    raw_code: bytes
    glyph_id: int
    width_1000: float
    word_space: bool = False
    cid: int | None = None
    vertical_metric: VerticalMetric | None = None


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise CFFPDFFontError(f"{label} is not a dictionary")
    return value


def _array(doc: PDFDocument, value: PDFObject | None, label: str) -> list[PDFObject]:
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise CFFPDFFontError(f"{label} is not an array")
    return value


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    if value is None:
        return default
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return default
    return float(value)


def _simple_widths(doc: PDFDocument, font: PDFDict) -> tuple[int, list[float], float]:
    first = int(_number(doc, font.get("FirstChar"), 0))
    last = int(_number(doc, font.get("LastChar"), 255))
    if not 0 <= first <= last <= 255:
        raise CFFPDFFontError("simple CFF FirstChar/LastChar range is invalid")
    raw = resolve(doc, font.get("Widths")) if font.get("Widths") is not None else None
    if not isinstance(raw, list):
        raise CFFPDFFontError("simple CFF font requires a Widths array")
    expected = last - first + 1
    if len(raw) != expected:
        raise CFFPDFFontError(
            f"simple CFF Widths has {len(raw)} entries, expected {expected}"
        )
    widths = [_number(doc, item, 0.0) for item in raw]
    descriptor = _dict(doc, font.get("FontDescriptor"), "FontDescriptor")
    missing = _number(doc, descriptor.get("MissingWidth"), 0.0)
    return first, widths, missing


def _cid_widths(doc: PDFDocument, cidfont: PDFDict) -> tuple[float, dict[int, float]]:
    default = _number(doc, cidfont.get("DW"), 1000.0)
    widths: dict[int, float] = {}
    raw = resolve(doc, cidfont.get("W")) if cidfont.get("W") is not None else None
    if raw is None:
        return default, widths
    if not isinstance(raw, list):
        raise CFFPDFFontError("CIDFont /W is not an array")
    index = 0
    while index < len(raw):
        start = int(_number(doc, raw[index], -1))
        index += 1
        if start < 0 or index >= len(raw):
            raise CFFPDFFontError("CIDFont /W array is malformed")
        second = resolve(doc, raw[index])
        index += 1
        if isinstance(second, list):
            for offset, value in enumerate(second):
                widths[start + offset] = _number(doc, value, default)
            continue
        end = int(_number(doc, second, -1))
        if end < start or index >= len(raw):
            raise CFFPDFFontError("CIDFont /W range is malformed")
        width = _number(doc, raw[index], default)
        index += 1
        for cid in range(start, end + 1):
            widths[cid] = width
    return default, widths


def _type0_cmap(doc: PDFDocument, font: PDFDict):
    try:
        return resolve_type0_cmap(doc, font.get("Encoding"))
    except CMapError as exc:
        raise CFFPDFFontError(
            f"Type0 Encoding CMap is invalid/unsupported: {exc}"
        ) from exc


def _cff_program(doc: PDFDocument, descriptor: PDFDict, expected_subtype: str) -> CFFFont:
    value = resolve(doc, descriptor.get("FontFile3")) if descriptor.get("FontFile3") is not None else None
    if not isinstance(value, PDFStream):
        raise CFFPDFFontError("CFF font program is not embedded in FontFile3")
    subtype = resolve(doc, value.get("Subtype")) if value.get("Subtype") is not None else None
    if not isinstance(subtype, PDFName) or subtype.value != expected_subtype:
        actual = subtype.value if isinstance(subtype, PDFName) else "missing"
        raise CFFPDFFontError(
            f"FontFile3 /Subtype shall be /{expected_subtype}, got /{actual}"
        )
    data = decoded_stream_bytes(doc, value, label=f"FontFile3/{expected_subtype}")
    try:
        return CFFFont(data)
    except (CFFError, UnsupportedCFFError) as exc:
        raise CFFPDFFontError(f"embedded CFF program is invalid/unsupported: {exc}") from exc


def _matrix(values: tuple[float, ...]) -> Matrix:
    if len(values) != 6:
        raise CFFPDFFontError("CFF FontMatrix shall contain six numbers")
    matrix = Matrix(*values)
    if abs(matrix.a * matrix.d - matrix.b * matrix.c) < 1e-18:
        raise CFFPDFFontError("CFF FontMatrix is singular")
    return matrix


class CFFPDFTextFont:
    """Resolve PDF Type1C or CIDFontType0C text into CFF glyphs."""

    is_type3 = False
    is_cff = True

    def __init__(self, doc: PDFDocument, font_value: PDFObject) -> None:
        self.doc = doc
        self.font = _dict(doc, font_value, "font resource")
        self.subtype = _name(resolve(doc, self.font.get("Subtype")))
        self.base_font = _name(resolve(doc, self.font.get("BaseFont")))
        self.vertical = False
        self.cff: CFFFont
        self._decode_impl = None
        self._init_font()

    def _init_font(self) -> None:
        if self.subtype == "Type1":
            descriptor = _dict(self.doc, self.font.get("FontDescriptor"), "FontDescriptor")
            self.cff = _cff_program(self.doc, descriptor, "Type1C")
            if self.cff.cid_keyed:
                raise CFFPDFFontError("/Type1 + /Type1C shall use a name-keyed CFF program")
            try:
                encoding = parse_encoding(self.doc, self.font.get("Encoding"), require_explicit=True)
            except FontEncodingError as exc:
                raise CFFPDFFontError(str(exc)) from exc
            first, widths, missing = _simple_widths(self.doc, self.font)

            def decode_simple(data: bytes) -> list[CFFGlyphItem]:
                result: list[CFFGlyphItem] = []
                for code in data:
                    name = encoding.get(code)
                    if name is None:
                        raise CFFPDFFontError(
                            f"simple CFF character code {code} has no owned encoding mapping"
                        )
                    gid = self.cff.glyph_id_for_name(name)
                    if gid is None:
                        gid = 0
                    index = code - first
                    width = widths[index] if 0 <= index < len(widths) else missing
                    result.append(
                        CFFGlyphItem(
                            raw_code=bytes([code]),
                            glyph_id=gid,
                            width_1000=width,
                            word_space=code == 32,
                        )
                    )
                return result

            self._decode_impl = decode_simple
            return

        if self.subtype == "Type0":
            descendants = _array(self.doc, self.font.get("DescendantFonts"), "DescendantFonts")
            if len(descendants) != 1:
                raise CFFPDFFontError("Type0 font must have exactly one descendant CIDFont")
            cidfont = _dict(self.doc, descendants[0], "descendant CIDFont")
            cid_subtype = _name(resolve(self.doc, cidfont.get("Subtype")))
            if cid_subtype != "CIDFontType0":
                raise CFFPDFFontError(
                    f"owned CFF renderer requires CIDFontType0, got /{cid_subtype or 'unknown'}"
                )
            descriptor = _dict(self.doc, cidfont.get("FontDescriptor"), "CIDFont FontDescriptor")
            self.cff = _cff_program(self.doc, descriptor, "CIDFontType0C")
            if not self.cff.cid_keyed:
                raise CFFPDFFontError(
                    "/CIDFontType0C requires a CID-keyed CFF program for owned CID mapping"
                )
            cmap = _type0_cmap(self.doc, self.font)
            self.vertical = cmap.vertical
            default_width, widths = _cid_widths(self.doc, cidfont)
            try:
                vertical_metrics = VerticalMetrics(self.doc, cidfont) if self.vertical else None
            except VerticalMetricsError as exc:
                raise CFFPDFFontError(f"CIDFont vertical metrics are invalid: {exc}") from exc

            def decode_type0(data: bytes) -> list[CFFGlyphItem]:
                result: list[CFFGlyphItem] = []
                for raw_code, cid in cmap.decode(data):
                    gid = self.cff.glyph_id_for_cid(cid)
                    if gid is None:
                        gid = 0
                    horizontal_width = widths.get(cid, default_width)
                    metric = (
                        vertical_metrics.metric(cid, horizontal_width)
                        if vertical_metrics is not None
                        else None
                    )
                    result.append(
                        CFFGlyphItem(
                            raw_code=raw_code,
                            glyph_id=gid,
                            width_1000=horizontal_width,
                            word_space=False,
                            cid=cid,
                            vertical_metric=metric,
                        )
                    )
                return result

            self._decode_impl = decode_type0
            return

        raise CFFPDFFontError(
            f"owned CFF renderer does not render PDF font subtype /{self.subtype or 'unknown'}"
        )

    def decode(self, data: bytes) -> list[CFFGlyphItem]:
        assert self._decode_impl is not None
        return self._decode_impl(data)

    def glyph_matrix(self, gid: int) -> Matrix:
        per_fd = self.cff.fd_font_matrix(gid)
        if per_fd is not None:
            raise CFFPDFFontError(
                "CID-keyed CFF per-FD FontMatrix requires owned matrix-composition support"
            )
        return _matrix(self.cff.font_matrix)

    def glyph_path(self, gid: int, transform: Matrix) -> Path:
        try:
            outline = self.cff.outline(gid)
        except (CFFError, UnsupportedCFFError) as exc:
            raise CFFPDFFontError(f"CFF glyph {gid} cannot be interpreted: {exc}") from exc
        matrix = transform.concat(self.glyph_matrix(gid))
        path = Path()
        for command in outline.commands:
            if command.operator == "M":
                path.move_to(*matrix.transform(command.values[0], command.values[1]))
            elif command.operator == "L":
                path.line_to(*matrix.transform(command.values[0], command.values[1]))
            elif command.operator == "C":
                p1 = matrix.transform(command.values[0], command.values[1])
                p2 = matrix.transform(command.values[2], command.values[3])
                p3 = matrix.transform(command.values[4], command.values[5])
                path.curve_to(*p1, *p2, *p3, tolerance=0.25)
            elif command.operator == "Z":
                path.close()
            else:
                raise CFFPDFFontError(f"unsupported CFF outline command {command.operator!r}")
        return path
