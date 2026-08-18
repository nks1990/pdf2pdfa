"""Safe font embedding on top of the owned SFNT parser."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Mapping

from .document import PDFDocument
from .objects import PDFDict, PDFName, PDFObject, PDFRef, PDFStream
from .structure import resolve, walk_reachable_objects
from .ttf import FontParseError, SFNTFont


class FontEmbeddingError(RuntimeError):
    pass


_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def normalized_pdf_font_name(value: str) -> str:
    value = value.lstrip("/")
    return _SUBSET_PREFIX.sub("", value)


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


def font_is_embedded(doc: PDFDocument, font: PDFDict) -> bool:
    subtype = _name(resolve(doc, font.get("Subtype")))
    if subtype == "Type3":
        return True
    descriptor = _dict(doc, font.get("FontDescriptor"))
    if subtype == "Type0":
        descendants = resolve(doc, font.get("DescendantFonts"))
        if isinstance(descendants, list) and len(descendants) == 1:
            descendant = _dict(doc, descendants[0])
            descriptor = _dict(doc, descendant.get("FontDescriptor")) if descendant else None
    return bool(
        descriptor
        and any(_stream(doc, descriptor.get(key)) is not None for key in ("FontFile", "FontFile2", "FontFile3"))
    )


@dataclass(frozen=True, slots=True)
class FontProgram:
    source: str
    data: bytes
    font: SFNTFont

    @classmethod
    def load(cls, source: str | Path | bytes) -> "FontProgram":
        if isinstance(source, (str, Path)):
            path = Path(source)
            data = path.read_bytes()
            label = str(path)
        else:
            data = bytes(source)
            label = "<bytes>"
        font = SFNTFont(data)
        if not font.embeddable:
            raise FontEmbeddingError(
                f"font {font.postscript_name or label} forbids outline embedding via OS/2 fsType=0x{font.embedding_fstype:04x}"
            )
        return cls(label, data, font)


class FontProgramMap:
    """Explicit font programs indexed by internal PostScript name.

    There is deliberately no system-font fallback.  A caller may populate this
    map from files it owns/has licensed, but the native engine never searches
    operating-system font directories or substitutes unrelated faces.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, FontProgram] = {}

    def add(self, source: str | Path | bytes) -> FontProgram:
        program = FontProgram.load(source)
        postscript = program.font.postscript_name
        if not postscript:
            raise FontEmbeddingError(f"font {program.source} has no PostScript name")
        self._by_name[normalized_pdf_font_name(postscript)] = program
        return program

    def add_directory(self, directory: str | Path) -> list[str]:
        errors: list[str] = []
        for path in sorted(Path(directory).iterdir()):
            if not path.is_file() or path.suffix.lower() not in (".ttf", ".otf"):
                continue
            try:
                self.add(path)
            except (FontParseError, FontEmbeddingError, OSError) as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def get(self, pdf_base_font: str) -> FontProgram | None:
        return self._by_name.get(normalized_pdf_font_name(pdf_base_font))

    def __bool__(self) -> bool:
        return bool(self._by_name)


@dataclass(frozen=True, slots=True)
class FontEmbeddingReport:
    embedded: int
    already_embedded: int
    type3: int
    missing: tuple[str, ...]
    unsupported: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing and not self.unsupported


def _descriptor_flags(font: SFNTFont, *, symbolic: bool) -> int:
    metrics = font.metrics
    flags = 0
    if metrics.fixed_pitch:
        flags |= 1
    flags |= 4 if symbolic else 32
    if metrics.italic_angle != 0:
        flags |= 64
    return flags


def _new_descriptor(
    doc: PDFDocument,
    *,
    pdf_font_name: str,
    program: FontProgram,
    symbolic: bool,
) -> PDFDict:
    metrics = program.font.metrics
    descriptor = PDFDict(
        {
            "Type": PDFName("FontDescriptor"),
            "FontName": PDFName(pdf_font_name),
            "Flags": _descriptor_flags(program.font, symbolic=symbolic),
            "FontBBox": list(metrics.pdf_bbox),
            "ItalicAngle": metrics.italic_angle,
            "Ascent": metrics.pdf_ascent,
            "Descent": metrics.pdf_descent,
            "CapHeight": metrics.pdf_cap_height,
            "StemV": max(50, min(250, int(round(metrics.weight_class / 5)))),
        }
    )
    if program.font.is_truetype:
        file_stream = PDFStream(PDFDict({"Length1": len(program.data)}), program.data)
        descriptor["FontFile2"] = doc.new_object(file_stream)
    elif program.font.is_cff:
        file_stream = PDFStream(
            PDFDict({"Subtype": PDFName("OpenType"), "Length1": len(program.data)}),
            program.data,
        )
        descriptor["FontFile3"] = doc.new_object(file_stream)
    else:
        raise FontEmbeddingError("unsupported SFNT outline flavor")
    return descriptor


def _font_name(font: PDFDict, doc: PDFDocument) -> str:
    base = resolve(doc, font.get("BaseFont"))
    return _name(base)


def _assert_program_identity(pdf_name: str, program: FontProgram) -> str:
    internal = program.font.postscript_name
    if not internal:
        raise FontEmbeddingError(f"font program {program.source} has no PostScript name")
    expected = normalized_pdf_font_name(pdf_name)
    actual = normalized_pdf_font_name(internal)
    if expected != actual:
        raise FontEmbeddingError(
            f"font identity mismatch: PDF requests {expected!r}, supplied program is {actual!r}"
        )
    return actual


def _embed_simple_true_type(
    doc: PDFDocument,
    font: PDFDict,
    program: FontProgram,
    pdf_name: str,
) -> None:
    if not program.font.is_truetype:
        raise FontEmbeddingError(
            f"simple /TrueType font {pdf_name} requires TrueType glyf outlines, not CFF/OpenType"
        )
    encoding = resolve(doc, font.get("Encoding")) if font.get("Encoding") is not None else None
    if isinstance(encoding, PDFDict):
        base_encoding = _name(resolve(doc, encoding.get("BaseEncoding")))
        if base_encoding != "WinAnsiEncoding" or encoding.get("Differences") is not None:
            raise FontEmbeddingError(
                f"{pdf_name}: custom TrueType encoding cannot be proven safe for in-place embedding"
            )
    elif encoding is not None and _name(encoding) != "WinAnsiEncoding":
        raise FontEmbeddingError(
            f"{pdf_name}: only WinAnsiEncoding is currently proven safe for simple TrueType embedding"
        )
    actual = _assert_program_identity(pdf_name, program)
    descriptor = _dict(doc, font.get("FontDescriptor"))
    if descriptor is None:
        descriptor = _new_descriptor(
            doc,
            pdf_font_name=actual,
            program=program,
            symbolic=False,
        )
        font["FontDescriptor"] = doc.new_object(descriptor)
    else:
        file_stream = PDFStream(PDFDict({"Length1": len(program.data)}), program.data)
        descriptor["FontFile2"] = doc.new_object(file_stream)
        descriptor["FontName"] = PDFName(actual)
    font["BaseFont"] = PDFName(actual)
    font["Subtype"] = PDFName("TrueType")


def _embed_type0(
    doc: PDFDocument,
    font: PDFDict,
    program: FontProgram,
    pdf_name: str,
) -> None:
    actual = _assert_program_identity(pdf_name, program)
    descendants = resolve(doc, font.get("DescendantFonts"))
    if not isinstance(descendants, list) or len(descendants) != 1:
        raise FontEmbeddingError(f"{pdf_name}: Type0 font must contain exactly one descendant")
    descendant = _dict(doc, descendants[0])
    if descendant is None:
        raise FontEmbeddingError(f"{pdf_name}: descendant CIDFont is malformed")
    subtype = _name(resolve(doc, descendant.get("Subtype")))
    if subtype == "CIDFontType2":
        if not program.font.is_truetype:
            raise FontEmbeddingError(f"{pdf_name}: CIDFontType2 requires TrueType outlines")
    elif subtype == "CIDFontType0":
        if not program.font.is_cff:
            raise FontEmbeddingError(f"{pdf_name}: CIDFontType0 requires CFF/OpenType outlines")
    else:
        raise FontEmbeddingError(f"{pdf_name}: unsupported descendant subtype /{subtype}")

    # The original Encoding/CIDToGIDMap remains untouched. Exact font identity
    # is required because those structures define the original CID -> glyph
    # selection; replacing them would risk text corruption.
    descriptor = _dict(doc, descendant.get("FontDescriptor"))
    if descriptor is None:
        descriptor = _new_descriptor(
            doc,
            pdf_font_name=actual,
            program=program,
            symbolic=True,
        )
        descendant["FontDescriptor"] = doc.new_object(descriptor)
    else:
        if program.font.is_truetype:
            stream = PDFStream(PDFDict({"Length1": len(program.data)}), program.data)
            descriptor["FontFile2"] = doc.new_object(stream)
        else:
            stream = PDFStream(
                PDFDict({"Subtype": PDFName("OpenType"), "Length1": len(program.data)}),
                program.data,
            )
            descriptor["FontFile3"] = doc.new_object(stream)
        descriptor["FontName"] = PDFName(actual)
    font["BaseFont"] = PDFName(actual)
    descendant["BaseFont"] = PDFName(actual)


def embed_missing_fonts(
    doc: PDFDocument,
    programs: FontProgramMap,
) -> FontEmbeddingReport:
    embedded = 0
    already = 0
    type3 = 0
    missing: list[str] = []
    unsupported: list[str] = []
    seen: set[str] = set()

    for path, value in walk_reachable_objects(doc):
        font = value if isinstance(value, PDFDict) else None
        if font is None or _name(resolve(doc, font.get("Type"))) != "Font":
            continue
        subtype = _name(resolve(doc, font.get("Subtype")))
        pdf_name = _font_name(font, doc) or f"<unnamed:{path}>"
        identity = str(value) if isinstance(value, PDFRef) else path
        # walk_reachable_objects yields resolved dictionaries, so indirect
        # identity is represented by path. BaseFont+subtype avoids duplicate
        # work for a resource reachable through multiple paths.
        key = f"{subtype}:{pdf_name}:{id(font)}"
        if key in seen:
            continue
        seen.add(key)
        if subtype == "Type3":
            type3 += 1
            continue
        if font_is_embedded(doc, font):
            already += 1
            continue
        program = programs.get(pdf_name)
        if program is None:
            missing.append(pdf_name)
            continue
        try:
            if subtype == "TrueType":
                _embed_simple_true_type(doc, font, program, pdf_name)
            elif subtype == "Type0":
                _embed_type0(doc, font, program, pdf_name)
            else:
                raise FontEmbeddingError(
                    f"{pdf_name}: native SFNT embedding does not rewrite /{subtype or 'unknown'} font dictionaries"
                )
        except FontEmbeddingError as exc:
            unsupported.append(str(exc))
            continue
        embedded += 1

    return FontEmbeddingReport(
        embedded=embedded,
        already_embedded=already,
        type3=type3,
        missing=tuple(dict.fromkeys(missing)),
        unsupported=tuple(dict.fromkeys(unsupported)),
    )
