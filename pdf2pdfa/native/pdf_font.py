"""PDF font dictionaries interpreted for owned TrueType rendering."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .cmap import CIDCMap, CMapError
from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .structure import decoded_stream_bytes, resolve
from .truetype import TrueTypeOutlines
from .ttf import FontParseError, SFNTFont


class PDFFontError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GlyphItem:
    raw_code: bytes
    glyph_id: int
    width_1000: float
    word_space: bool = False
    cid: int | None = None


def _name(value: PDFObject | None) -> str:
    return value.value if isinstance(value, PDFName) else ""


def _dict(doc: PDFDocument, value: PDFObject | None, label: str) -> PDFDict:
    value = resolve(doc, value)
    if not isinstance(value, PDFDict):
        raise PDFFontError(f"{label} is not a dictionary")
    return value


def _array(doc: PDFDocument, value: PDFObject | None, label: str) -> list[PDFObject]:
    value = resolve(doc, value)
    if not isinstance(value, list):
        raise PDFFontError(f"{label} is not an array")
    return value


def _number(doc: PDFDocument, value: PDFObject | None, default: float = 0.0) -> float:
    if value is None:
        return default
    value = resolve(doc, value)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, Decimal)):
        return float(value)
    return default


def _font_program(doc: PDFDocument, descriptor: PDFDict) -> tuple[SFNTFont, TrueTypeOutlines]:
    stream: PDFStream | None = None
    for key in ("FontFile2", "FontFile3"):
        candidate = resolve(doc, descriptor.get(key)) if descriptor.get(key) is not None else None
        if isinstance(candidate, PDFStream):
            stream = candidate
            break
    if stream is None:
        raise PDFFontError("font program is not embedded")
    data = decoded_stream_bytes(doc, stream, label="embedded font program")
    try:
        sfnt = SFNTFont(data)
    except FontParseError as exc:
        raise PDFFontError(f"embedded font program is not supported SFNT: {exc}") from exc
    if not sfnt.is_truetype:
        raise PDFFontError("embedded font uses CFF outlines; use the owned CFF renderer")
    try:
        outlines = TrueTypeOutlines(sfnt)
    except FontParseError as exc:
        raise PDFFontError(f"TrueType outline tables are invalid: {exc}") from exc
    return sfnt, outlines


def _winansi_unicode(code: int) -> int:
    try:
        return ord(bytes([code]).decode("cp1252"))
    except UnicodeDecodeError:
        return code


def _simple_widths(doc: PDFDocument, font: PDFDict) -> tuple[int, list[float], float]:
    first = int(_number(doc, font.get("FirstChar"), 0))
    widths_value = resolve(doc, font.get("Widths")) if font.get("Widths") is not None else None
    widths: list[float] = []
    if isinstance(widths_value, list):
        for value in widths_value:
            widths.append(_number(doc, value, 0.0))
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
        raise PDFFontError("CIDFont /W is not an array")
    index = 0
    while index < len(raw):
        start = int(_number(doc, raw[index], -1))
        index += 1
        if start < 0 or index >= len(raw):
            raise PDFFontError("CIDFont /W array is malformed")
        second = resolve(doc, raw[index])
        index += 1
        if isinstance(second, list):
            for offset, value in enumerate(second):
                widths[start + offset] = _number(doc, value, default)
            continue
        end = int(_number(doc, second, -1))
        if end < start or index >= len(raw):
            raise PDFFontError("CIDFont /W range is malformed")
        width = _number(doc, raw[index], default)
        index += 1
        for cid in range(start, end + 1):
            widths[cid] = width
    return default, widths


def _cid_to_gid(doc: PDFDocument, cidfont: PDFDict):
    value = resolve(doc, cidfont.get("CIDToGIDMap")) if cidfont.get("CIDToGIDMap") is not None else PDFName("Identity")
    if isinstance(value, PDFName):
        if value.value != "Identity":
            raise PDFFontError(f"unsupported CIDToGIDMap name /{value.value}")
        return lambda cid: cid
    if isinstance(value, PDFStream):
        table = decoded_stream_bytes(doc, value, label="CIDToGIDMap")

        def lookup(cid: int) -> int:
            offset = cid * 2
            if offset + 2 > len(table):
                return 0
            return int.from_bytes(table[offset : offset + 2], "big")

        return lookup
    raise PDFFontError("CIDToGIDMap is neither /Identity nor a stream")


def _type0_cmap(doc: PDFDocument, font: PDFDict) -> CIDCMap:
    encoding = resolve(doc, font.get("Encoding"))
    if isinstance(encoding, PDFName):
        if encoding.value == "Identity-H":
            return CIDCMap.identity(vertical=False)
        if encoding.value == "Identity-V":
            return CIDCMap.identity(vertical=True)
        raise PDFFontError(
            f"predefined CMap /{encoding.value} is not yet bundled in the owned renderer"
        )
    if isinstance(encoding, PDFStream):
        try:
            return CIDCMap.parse(decoded_stream_bytes(doc, encoding, label="Type0 Encoding CMap"))
        except CMapError as exc:
            raise PDFFontError(f"Type0 Encoding CMap is invalid: {exc}") from exc
    raise PDFFontError("Type0 font Encoding is missing or invalid")


class PDFTextFont:
    def __init__(self, doc: PDFDocument, font_value: PDFObject) -> None:
        self.doc = doc
        self.font = _dict(doc, font_value, "font resource")
        self.subtype = _name(resolve(doc, self.font.get("Subtype")))
        self.base_font = _name(resolve(doc, self.font.get("BaseFont")))
        self.vertical = False
        self.sfnt: SFNTFont
        self.outlines: TrueTypeOutlines
        self._decode_impl = None
        self._init_font()

    def _init_font(self) -> None:
        if self.subtype == "TrueType":
            encoding = resolve(self.doc, self.font.get("Encoding")) if self.font.get("Encoding") is not None else None
            if not isinstance(encoding, PDFName) or encoding.value != "WinAnsiEncoding":
                raise PDFFontError(
                    f"simple TrueType {self.base_font or '<unnamed>'} requires WinAnsiEncoding in owned renderer"
                )
            descriptor = _dict(self.doc, self.font.get("FontDescriptor"), "FontDescriptor")
            self.sfnt, self.outlines = _font_program(self.doc, descriptor)
            cmap = self.sfnt.cmap()
            first, widths, missing = _simple_widths(self.doc, self.font)

            def decode_simple(data: bytes) -> list[GlyphItem]:
                result: list[GlyphItem] = []
                for code in data:
                    unicode_codepoint = _winansi_unicode(code)
                    gid = cmap.get(unicode_codepoint, 0)
                    index = code - first
                    width = widths[index] if 0 <= index < len(widths) else missing
                    if width == 0 and gid:
                        width = self.sfnt.glyph_advance_1000(gid)
                    result.append(
                        GlyphItem(
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
                raise PDFFontError("Type0 font must have exactly one descendant CIDFont")
            cidfont = _dict(self.doc, descendants[0], "descendant CIDFont")
            cid_subtype = _name(resolve(self.doc, cidfont.get("Subtype")))
            if cid_subtype != "CIDFontType2":
                raise PDFFontError(
                    f"owned TrueType renderer requires CIDFontType2, got /{cid_subtype or 'unknown'}"
                )
            descriptor = _dict(self.doc, cidfont.get("FontDescriptor"), "CIDFont FontDescriptor")
            self.sfnt, self.outlines = _font_program(self.doc, descriptor)
            cmap = _type0_cmap(self.doc, self.font)
            self.vertical = cmap.vertical
            if self.vertical:
                raise PDFFontError("vertical Type0 text requires owned vertical-metrics renderer")
            default_width, widths = _cid_widths(self.doc, cidfont)
            gid_for_cid = _cid_to_gid(self.doc, cidfont)

            def decode_type0(data: bytes) -> list[GlyphItem]:
                result: list[GlyphItem] = []
                for raw_code, cid in cmap.decode(data):
                    gid = gid_for_cid(cid)
                    result.append(
                        GlyphItem(
                            raw_code=raw_code,
                            glyph_id=gid,
                            width_1000=widths.get(cid, default_width),
                            word_space=False,
                            cid=cid,
                        )
                    )
                return result

            self._decode_impl = decode_type0
            return

        raise PDFFontError(
            f"owned TrueType renderer does not render PDF font subtype /{self.subtype or 'unknown'}"
        )

    def decode(self, data: bytes) -> list[GlyphItem]:
        assert self._decode_impl is not None
        return self._decode_impl(data)
