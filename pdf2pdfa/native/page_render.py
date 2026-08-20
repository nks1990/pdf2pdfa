"""Owned PDF page/content interpreter built on the native raster engine.

The renderer is fail-closed: painting operators or resource types that are not
implemented raise ``UnsupportedRenderingError`` rather than being skipped.
That property is critical because rendered output is later used for PDF/A-1
transparency flattening and fidelity decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math
from pathlib import Path as FSPath
from typing import Iterable

from .color import ColorSpace, ColorSpaceError, parse_color_space
from .content import ContentInstruction, InlineImage, parse_content_stream
from .document import PDFDocument
from .image import DecodedImage, ImageError, decode_image
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .pdf_font import PDFTextFont, PDFFontError
from .raster import Color, Matrix, Path, Surface, rasterize_fill
from .structure import PageInfo, decoded_stream_bytes, page_content_bytes, resolve, walk_pages
from .text_render import TextPaintStyle, TextRenderError, TrueTypeTextRenderer


class RenderingError(RuntimeError):
    pass


class UnsupportedRenderingError(RenderingError):
    pass


@dataclass(slots=True)
class GraphicsState:
    ctm: Matrix
    fill_space: ColorSpace
    stroke_space: ColorSpace
    fill_color: Color = Color(0, 0, 0, 1)
    stroke_color: Color = Color(0, 0, 0, 1)
    fill_alpha: float = 1.0
    stroke_alpha: float = 1.0
    blend_mode: str = "Normal"
    line_width: float = 1.0
    line_cap: int = 0
    line_join: int = 0
    miter_limit: float = 10.0
    dash_array: tuple[float, ...] = ()
    dash_phase: float = 0.0
    rendering_intent: str = "RelativeColorimetric"
    flatness: float = 1.0
    clip: bytearray = field(default_factory=bytearray)

    def clone(self) -> "GraphicsState":
        return GraphicsState(
            ctm=self.ctm,
            fill_space=self.fill_space,
            stroke_space=self.stroke_space,
            fill_color=self.fill_color,
            stroke_color=self.stroke_color,
            fill_alpha=self.fill_alpha,
            stroke_alpha=self.stroke_alpha,
            blend_mode=self.blend_mode,
            line_width=self.line_width,
            line_cap=self.line_cap,
            line_join=self.line_join,
            miter_limit=self.miter_limit,
            dash_array=self.dash_array,
            dash_phase=self.dash_phase,
            rendering_intent=self.rendering_intent,
            flatness=self.flatness,
            clip=bytearray(self.clip),
        )


@dataclass(frozen=True, slots=True)
class RenderedPage:
    width: int
    height: int
    dpi: int
    crop_box: tuple[float, float, float, float]
    rotate: int
    surface: Surface

    def rgb_bytes(self) -> bytes:
        return self.surface.rgb_bytes()


_BLEND_MODES = {
    "Normal", "Compatible", "Multiply", "Screen", "Overlay", "Darken",
    "Lighten", "ColorDodge", "ColorBurn", "HardLight", "SoftLight",
    "Difference", "Exclusion", "Hue", "Saturation", "Color", "Luminosity",
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _number(value: PDFObject, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise RenderingError(f"{label} expects numeric operands")
    return float(value)


def _integer(value: PDFObject, label: str) -> int:
    number = _number(value, label)
    integer = int(number)
    if integer != number:
        raise RenderingError(f"{label} expects an integer")
    return integer


def _name(value: PDFObject, label: str) -> str:
    if not isinstance(value, PDFName):
        raise RenderingError(f"{label} expects a name")
    return value.value


def _array(value: PDFObject, label: str) -> list[PDFObject]:
    if not isinstance(value, list):
        raise RenderingError(f"{label} expects an array")
    return value


def _resource_dict(
    doc: PDFDocument,
    resources: PDFDict | None,
    category: str,
) -> PDFDict:
    if resources is None or resources.get(category) is None:
        raise RenderingError(f"page resources have no /{category} dictionary")
    value = resolve(doc, resources.get(category))
    if not isinstance(value, PDFDict):
        raise RenderingError(f"resource /{category} is not a dictionary")
    return value


def _resolve_resource(
    doc: PDFDocument,
    resources: PDFDict | None,
    category: str,
    name: str,
) -> PDFObject:
    table = _resource_dict(doc, resources, category)
    if name not in table:
        raise RenderingError(f"resource /{category} has no /{name}")
    return resolve(doc, table[name])


def _initial_ctm(page: PageInfo, dpi: int) -> tuple[Matrix, int, int, tuple[float, float, float, float]]:
    x0, y0, x1, y1 = (float(value) for value in page.crop_box)
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        raise RenderingError("page CropBox has non-positive dimensions")
    scale = dpi / 72.0
    rotate = page.rotate % 360
    if rotate == 0:
        matrix = Matrix(scale, 0, 0, -scale, -x0 * scale, y1 * scale)
        pixel_width = math.ceil(width * scale)
        pixel_height = math.ceil(height * scale)
    elif rotate == 90:
        # Clockwise display rotation: (x,y) -> (y-y0, x-x0), then raster y-down.
        matrix = Matrix(0, scale, scale, 0, -y0 * scale, -x0 * scale)
        pixel_width = math.ceil(height * scale)
        pixel_height = math.ceil(width * scale)
    elif rotate == 180:
        matrix = Matrix(-scale, 0, 0, scale, x1 * scale, -y0 * scale)
        pixel_width = math.ceil(width * scale)
        pixel_height = math.ceil(height * scale)
    elif rotate == 270:
        matrix = Matrix(0, -scale, -scale, 0, y1 * scale, x1 * scale)
        pixel_width = math.ceil(height * scale)
        pixel_height = math.ceil(width * scale)
    else:
        raise UnsupportedRenderingError(
            f"page Rotate {page.rotate} is not a multiple of 90 degrees"
        )
    if pixel_width <= 0 or pixel_height <= 0 or pixel_width * pixel_height > 250_000_000:
        raise RenderingError("page raster dimensions are invalid/unsafe")
    return matrix, pixel_width, pixel_height, (x0, y0, x1, y1)


def _similarity_scale(matrix: Matrix) -> float | None:
    # A PDF stroke can be represented by a scalar device width only when the
    # CTM linear part is a rotation/reflection times a uniform scale.
    column1 = math.hypot(matrix.a, matrix.b)
    column2 = math.hypot(matrix.c, matrix.d)
    dot = matrix.a * matrix.c + matrix.b * matrix.d
    tolerance = 1e-7 * max(1.0, column1, column2)
    if abs(column1 - column2) > tolerance or abs(dot) > tolerance:
        return None
    return (column1 + column2) * 0.5


def _inline_dictionary(dictionary: PDFDict) -> PDFDict:
    key_alias = {
        "BPC": "BitsPerComponent", "CS": "ColorSpace", "D": "Decode",
        "DP": "DecodeParms", "F": "Filter", "H": "Height", "IM": "ImageMask",
        "I": "Interpolate", "W": "Width",
    }
    cs_alias = {
        "G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed",
    }
    filter_alias = {
        "AHx": "ASCIIHexDecode", "A85": "ASCII85Decode", "LZW": "LZWDecode",
        "Fl": "FlateDecode", "RL": "RunLengthDecode", "CCF": "CCITTFaxDecode",
        "DCT": "DCTDecode",
    }
    result = PDFDict()
    for key, value in dictionary.items():
        canonical = key_alias.get(key, key)
        if canonical == "ColorSpace" and isinstance(value, PDFName):
            value = PDFName(cs_alias.get(value.value, value.value))
        if canonical == "Filter":
            if isinstance(value, PDFName):
                value = PDFName(filter_alias.get(value.value, value.value))
            elif isinstance(value, list):
                value = [
                    PDFName(filter_alias.get(item.value, item.value)) if isinstance(item, PDFName) else item
                    for item in value
                ]
        result[canonical] = value
    result["Subtype"] = PDFName("Image")
    return result


class PageRenderer:
    def __init__(self, doc: PDFDocument, *, dpi: int = 144) -> None:
        if dpi <= 0 or dpi > 2400:
            raise ValueError("dpi must be between 1 and 2400")
        self.doc = doc
        self.dpi = dpi
        self.surface: Surface | None = None
        self.state: GraphicsState | None = None
        self.stack: list[GraphicsState] = []
        self.path = Path()
        self.pending_clip: bool | None = None
        self.resources: PDFDict | None = None
        self.text: TrueTypeTextRenderer | None = None
        self._form_stack: set[int] = set()

    def render_page(self, page: PageInfo) -> RenderedPage:
        ctm, width, height, crop = _initial_ctm(page, self.dpi)
        self.surface = Surface(width, height, background=Color(1, 1, 1, 1))
        gray = ColorSpace("DeviceGray", 1, lambda values: (values[0], values[0], values[0]))
        self.state = GraphicsState(
            ctm=ctm,
            fill_space=gray,
            stroke_space=gray,
            clip=bytearray(b"\xff" * (width * height)),
        )
        self.surface.clip[:] = self.state.clip
        self.stack.clear()
        self.path.clear()
        self.pending_clip = None
        self.resources = page.resources
        self.text = TrueTypeTextRenderer(self.surface, ctm=ctm)
        self._execute(page_content_bytes(self.doc, page), page.resources)
        if self.stack:
            raise RenderingError("content stream ended with unbalanced q/Q")
        if self.text.in_text_object:
            raise RenderingError("content stream ended inside BT/ET")
        return RenderedPage(width, height, self.dpi, crop, page.rotate, self.surface)

    def _require(self) -> tuple[Surface, GraphicsState, TrueTypeTextRenderer]:
        if self.surface is None or self.state is None or self.text is None:
            raise RenderingError("renderer has no active page")
        return self.surface, self.state, self.text

    def _execute(self, content: bytes, resources: PDFDict | None) -> None:
        previous_resources = self.resources
        self.resources = resources
        try:
            for item in parse_content_stream(content):
                if isinstance(item, InlineImage):
                    self._inline_image(item)
                else:
                    self._instruction(item)
        finally:
            self.resources = previous_resources

    def _instruction(self, instruction: ContentInstruction) -> None:
        surface, state, text = self._require()
        op = instruction.operator
        args = instruction.operands

        if op == "q":
            if args: raise RenderingError("q takes no operands")
            state.clip[:] = surface.clip
            self.stack.append(state.clone())
            return
        if op == "Q":
            if args: raise RenderingError("Q takes no operands")
            if not self.stack: raise RenderingError("Q without matching q")
            self.state = self.stack.pop()
            surface.clip[:] = self.state.clip
            text.ctm = self.state.ctm
            self.path.clear(); self.pending_clip = None
            return
        if op == "cm":
            if len(args) != 6: raise RenderingError("cm expects six numbers")
            matrix = Matrix(*(_number(value, "cm") for value in args))
            state.ctm = state.ctm.concat(matrix)
            text.ctm = state.ctm
            return

        if op in {"m", "l", "c", "v", "y", "h", "re"}:
            self._path_operator(op, args)
            return
        if op in {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}:
            self._paint_operator(op, args)
            return
        if op in {"W", "W*"}:
            if args: raise RenderingError(f"{op} takes no operands")
            self.pending_clip = op == "W*"
            return

        if op == "w":
            if len(args) != 1: raise RenderingError("w expects one number")
            state.line_width = max(0.0, _number(args[0], "w")); return
        if op == "J":
            if len(args) != 1: raise RenderingError("J expects one integer")
            state.line_cap = _integer(args[0], "J")
            if state.line_cap not in (0,1,2): raise RenderingError("invalid line cap")
            return
        if op == "j":
            if len(args) != 1: raise RenderingError("j expects one integer")
            state.line_join = _integer(args[0], "j")
            if state.line_join not in (0,1,2): raise RenderingError("invalid line join")
            return
        if op == "M":
            if len(args) != 1: raise RenderingError("M expects one number")
            state.miter_limit = max(1.0, _number(args[0], "M")); return
        if op == "d":
            if len(args) != 2: raise RenderingError("d expects array and phase")
            array = _array(args[0], "d")
            dash = tuple(_number(value, "d") for value in array)
            if any(value < 0 for value in dash) or (dash and sum(dash) <= 0):
                raise RenderingError("invalid dash pattern")
            state.dash_array = dash
            state.dash_phase = _number(args[1], "d")
            return
        if op == "ri":
            if len(args) != 1: raise RenderingError("ri expects one name")
            state.rendering_intent = _name(args[0], "ri"); return
        if op == "i":
            if len(args) != 1: raise RenderingError("i expects one number")
            state.flatness = _clamp(_number(args[0], "i") / 100.0) * 100.0; return

        if op in {"G", "g", "RG", "rg", "K", "k"}:
            self._device_color(op, args); return
        if op in {"CS", "cs"}:
            if len(args) != 1: raise RenderingError(f"{op} expects a name")
            space = parse_color_space(self.doc, args[0], resources=self.resources)
            if op == "CS": state.stroke_space = space
            else: state.fill_space = space
            return
        if op in {"SC", "sc", "SCN", "scn"}:
            self._set_color(op, args); return

        if op == "gs":
            if len(args) != 1: raise RenderingError("gs expects one resource name")
            self._extgstate(_name(args[0], "gs")); return
        if op == "Do":
            if len(args) != 1: raise RenderingError("Do expects one resource name")
            self._xobject(_name(args[0], "Do")); return

        if op == "BT":
            if args: raise RenderingError("BT takes no operands")
            text.ctm = state.ctm; text.begin_text(); return
        if op == "ET":
            if args: raise RenderingError("ET takes no operands")
            text.end_text(); return
        if op == "Tf":
            if len(args) != 2: raise RenderingError("Tf expects font name and size")
            font_value = _resolve_resource(self.doc, self.resources, "Font", _name(args[0], "Tf"))
            try: font = PDFTextFont(self.doc, font_value)
            except PDFFontError as exc: raise UnsupportedRenderingError(str(exc)) from exc
            text.set_font(font, _number(args[1], "Tf")); return
        if op == "Tm":
            if len(args) != 6: raise RenderingError("Tm expects six numbers")
            text.set_text_matrix(Matrix(*(_number(value, "Tm") for value in args))); return
        if op == "Td":
            if len(args) != 2: raise RenderingError("Td expects two numbers")
            text.move_text(_number(args[0], "Td"), _number(args[1], "Td")); return
        if op == "TD":
            if len(args) != 2: raise RenderingError("TD expects two numbers")
            text.move_text_set_leading(_number(args[0], "TD"), _number(args[1], "TD")); return
        if op == "T*":
            if args: raise RenderingError("T* takes no operands")
            text.next_line(); return
        if op == "Tc":
            if len(args) != 1: raise RenderingError("Tc expects one number")
            text.set_char_spacing(_number(args[0], "Tc")); return
        if op == "Tw":
            if len(args) != 1: raise RenderingError("Tw expects one number")
            text.set_word_spacing(_number(args[0], "Tw")); return
        if op == "Tz":
            if len(args) != 1: raise RenderingError("Tz expects one number")
            text.set_horizontal_scale_percent(_number(args[0], "Tz")); return
        if op == "TL":
            if len(args) != 1: raise RenderingError("TL expects one number")
            text.set_leading(_number(args[0], "TL")); return
        if op == "Ts":
            if len(args) != 1: raise RenderingError("Ts expects one number")
            text.set_rise(_number(args[0], "Ts")); return
        if op == "Tr":
            if len(args) != 1: raise RenderingError("Tr expects one integer")
            text.set_render_mode(_integer(args[0], "Tr")); return
        if op in {"Tj", "TJ", "'", '"'}:
            self._show_text(op, args); return

        if op == "sh":
            raise UnsupportedRenderingError("shading operator requires the owned shading renderer")
        if op in {"BMC", "BDC", "EMC", "MP", "DP", "BX", "EX"}:
            # Marked-content and compatibility operators do not paint by themselves.
            return
        if op in {"d0", "d1"}:
            raise UnsupportedRenderingError("Type3 glyph metrics require the owned Type3 renderer")

        raise UnsupportedRenderingError(f"unsupported painting/content operator {op!r}")

    def _path_operator(self, op: str, args: list[PDFObject]) -> None:
        _, state, _ = self._require()
        transform = state.ctm.transform
        if op == "m":
            if len(args) != 2: raise RenderingError("m expects two numbers")
            self.path.move_to(*transform(_number(args[0], "m"), _number(args[1], "m"))); return
        if op == "l":
            if len(args) != 2: raise RenderingError("l expects two numbers")
            self.path.line_to(*transform(_number(args[0], "l"), _number(args[1], "l"))); return
        if op == "c":
            if len(args) != 6: raise RenderingError("c expects six numbers")
            p1 = transform(_number(args[0], "c"), _number(args[1], "c"))
            p2 = transform(_number(args[2], "c"), _number(args[3], "c"))
            p3 = transform(_number(args[4], "c"), _number(args[5], "c"))
            self.path.curve_to(*p1, *p2, *p3, tolerance=max(0.05, state.flatness)); return
        if op == "v":
            if len(args) != 4: raise RenderingError("v expects four numbers")
            current = self.path.current_point
            p2 = transform(_number(args[0], "v"), _number(args[1], "v"))
            p3 = transform(_number(args[2], "v"), _number(args[3], "v"))
            self.path.curve_to(*current, *p2, *p3, tolerance=max(0.05, state.flatness)); return
        if op == "y":
            if len(args) != 4: raise RenderingError("y expects four numbers")
            p1 = transform(_number(args[0], "y"), _number(args[1], "y"))
            p3 = transform(_number(args[2], "y"), _number(args[3], "y"))
            self.path.curve_to(*p1, *p3, *p3, tolerance=max(0.05, state.flatness)); return
        if op == "h":
            if args: raise RenderingError("h takes no operands")
            self.path.close(); return
        if op == "re":
            if len(args) != 4: raise RenderingError("re expects four numbers")
            x, y, w, h = (_number(value, "re") for value in args)
            corners = [transform(x,y), transform(x+w,y), transform(x+w,y+h), transform(x,y+h)]
            self.path.move_to(*corners[0])
            for point in corners[1:]: self.path.line_to(*point)
            self.path.close(); return

    def _apply_pending_clip(self) -> None:
        surface, state, _ = self._require()
        if self.pending_clip is None:
            return
        mask = rasterize_fill(self.path, surface.width, surface.height, even_odd=self.pending_clip)
        surface.apply_clip_mask(mask)
        state.clip[:] = surface.clip
        self.pending_clip = None

    def _paint_operator(self, op: str, args: list[PDFObject]) -> None:
        if args: raise RenderingError(f"{op} takes no operands")
        surface, state, _ = self._require()
        close = op in {"s", "b", "b*"}
        if close and self.path.subpaths:
            self.path.close()
        fill = op in {"f", "F", "f*", "B", "B*", "b", "b*"}
        stroke = op in {"S", "s", "B", "B*", "b", "b*"}
        even_odd = op in {"f*", "B*", "b*"}
        self._apply_pending_clip()
        if fill:
            color = Color(state.fill_color.r, state.fill_color.g, state.fill_color.b, state.fill_alpha)
            surface.fill_path(self.path, color, even_odd=even_odd, blend_mode=state.blend_mode)
        if stroke:
            if state.dash_array:
                raise UnsupportedRenderingError("dashed strokes require the owned dash stroke renderer")
            scale = _similarity_scale(state.ctm)
            if scale is None:
                raise UnsupportedRenderingError(
                    "stroked path under non-uniform/skew CTM requires affine stroke outline renderer"
                )
            color = Color(state.stroke_color.r, state.stroke_color.g, state.stroke_color.b, state.stroke_alpha)
            surface.stroke_path(
                self.path,
                color,
                stroke_width=max(0.01, state.line_width * scale),
                line_cap=state.line_cap,
                line_join=state.line_join,
                blend_mode=state.blend_mode,
            )
        self.path.clear(); self.pending_clip = None

    def _device_color(self, op: str, args: list[PDFObject]) -> None:
        _, state, _ = self._require()
        expected = 1 if op in {"G","g"} else 3 if op in {"RG","rg"} else 4
        if len(args) != expected: raise RenderingError(f"{op} expects {expected} numbers")
        values = tuple(_number(value, op) for value in args)
        if expected == 1:
            space = parse_color_space(self.doc, PDFName("DeviceGray"), resources=self.resources)
        elif expected == 3:
            space = parse_color_space(self.doc, PDFName("DeviceRGB"), resources=self.resources)
        else:
            space = parse_color_space(self.doc, PDFName("DeviceCMYK"), resources=self.resources)
        rgb = space.rgb(values)
        color = Color(*rgb, 1)
        if op[0].isupper(): state.stroke_space, state.stroke_color = space, color
        else: state.fill_space, state.fill_color = space, color

    def _set_color(self, op: str, args: list[PDFObject]) -> None:
        _, state, _ = self._require()
        stroke = op[0].isupper()
        space = state.stroke_space if stroke else state.fill_space
        if space.family == "Pattern":
            raise UnsupportedRenderingError("Pattern color requires the owned pattern renderer")
        if len(args) != space.components:
            raise RenderingError(f"{op} operand count does not match /{space.family}")
        rgb = space.rgb(tuple(_number(value, op) for value in args))
        color = Color(*rgb, 1)
        if stroke: state.stroke_color = color
        else: state.fill_color = color

    def _extgstate(self, name: str) -> None:
        _, state, _ = self._require()
        value = _resolve_resource(self.doc, self.resources, "ExtGState", name)
        if not isinstance(value, PDFDict): raise RenderingError("ExtGState resource is not a dictionary")
        for key, raw in value.items():
            raw = resolve(self.doc, raw)
            if key == "ca": state.fill_alpha = _clamp(_number(raw, "ExtGState/ca"))
            elif key == "CA": state.stroke_alpha = _clamp(_number(raw, "ExtGState/CA"))
            elif key == "BM":
                candidate = raw[0] if isinstance(raw, list) and raw else raw
                mode = _name(candidate, "ExtGState/BM")
                if mode not in _BLEND_MODES: raise UnsupportedRenderingError(f"unsupported blend mode /{mode}")
                state.blend_mode = mode
            elif key == "LW": state.line_width = max(0.0, _number(raw, "ExtGState/LW"))
            elif key == "LC": state.line_cap = _integer(raw, "ExtGState/LC")
            elif key == "LJ": state.line_join = _integer(raw, "ExtGState/LJ")
            elif key == "ML": state.miter_limit = max(1.0, _number(raw, "ExtGState/ML"))
            elif key == "D":
                if not isinstance(raw, list) or len(raw) != 2: raise RenderingError("ExtGState/D is malformed")
                dash = resolve(self.doc, raw[0]); phase = resolve(self.doc, raw[1])
                if not isinstance(dash, list): raise RenderingError("ExtGState/D dash array invalid")
                state.dash_array = tuple(_number(item, "ExtGState/D") for item in dash)
                state.dash_phase = _number(phase, "ExtGState/D")
            elif key == "RI": state.rendering_intent = _name(raw, "ExtGState/RI")
            elif key == "FL": state.flatness = max(0.0, min(100.0, _number(raw, "ExtGState/FL")))
            elif key == "SMask":
                if isinstance(raw, PDFName) and raw.value == "None": continue
                raise UnsupportedRenderingError("ExtGState soft masks require the owned transparency-group renderer")
            elif key in {"Type", "AIS", "TK", "OP", "op", "OPM", "BG", "BG2", "UCR", "UCR2", "TR", "TR2", "HT", "Font"}:
                # Some of these affect device/prepress rendering. Fail for entries
                # that can materially alter appearance; benign defaults are accepted.
                if key in {"TR", "TR2", "HT", "BG", "BG2", "UCR", "UCR2", "Font"}:
                    if not (key == "TR2" and isinstance(raw, PDFName) and raw.value == "Default"):
                        raise UnsupportedRenderingError(f"ExtGState /{key} requires dedicated owned renderer support")
            else:
                raise UnsupportedRenderingError(f"unknown ExtGState key /{key}")

    def _xobject(self, name: str) -> None:
        surface, state, _ = self._require()
        value = _resolve_resource(self.doc, self.resources, "XObject", name)
        if not isinstance(value, PDFStream): raise RenderingError("XObject is not a stream")
        subtype = _name(resolve(self.doc, value.get("Subtype")), "XObject/Subtype")
        if subtype == "Image":
            try: image = decode_image(self.doc, value, resources=self.resources)
            except ImageError as exc: raise UnsupportedRenderingError(str(exc)) from exc
            self._draw_image(image, state.ctm)
            return
        if subtype == "Form":
            self._form(value)
            return
        if subtype == "PS":
            raise UnsupportedRenderingError("PostScript XObjects are not renderable by the owned engine")
        raise UnsupportedRenderingError(f"unsupported XObject subtype /{subtype}")

    def _form(self, form: PDFStream) -> None:
        surface, state, text = self._require()
        identity = id(form)
        if identity in self._form_stack: raise RenderingError("recursive Form XObject cycle")
        if len(self._form_stack) >= 64: raise RenderingError("Form XObject recursion exceeds 64")
        group = resolve(self.doc, form.get("Group")) if form.get("Group") is not None else None
        if isinstance(group, PDFDict) and _name(resolve(self.doc, group.get("S")), "Form/Group/S") == "Transparency":
            raise UnsupportedRenderingError("transparency-group Form requires owned group compositor")
        matrix_value = resolve(self.doc, form.get("Matrix")) if form.get("Matrix") is not None else None
        form_matrix = Matrix()
        if matrix_value is not None:
            if not isinstance(matrix_value, list) or len(matrix_value) != 6: raise RenderingError("Form /Matrix is malformed")
            form_matrix = Matrix(*(_number(value, "Form/Matrix") for value in matrix_value))
        bbox_value = resolve(self.doc, form.get("BBox"))
        if not isinstance(bbox_value, list) or len(bbox_value) != 4: raise RenderingError("Form XObject requires BBox")
        bbox = [_number(value, "Form/BBox") for value in bbox_value]
        saved = state.clone(); saved_resources = self.resources
        self.stack.append(saved)
        try:
            state.ctm = state.ctm.concat(form_matrix); text.ctm = state.ctm
            clip_path = Path(); corners = [
                state.ctm.transform(bbox[0], bbox[1]), state.ctm.transform(bbox[2], bbox[1]),
                state.ctm.transform(bbox[2], bbox[3]), state.ctm.transform(bbox[0], bbox[3]),
            ]
            clip_path.move_to(*corners[0])
            for point in corners[1:]: clip_path.line_to(*point)
            clip_path.close()
            surface.apply_clip_mask(rasterize_fill(clip_path, surface.width, surface.height))
            state.clip[:] = surface.clip
            raw_resources = resolve(self.doc, form.get("Resources")) if form.get("Resources") is not None else saved_resources
            resources = raw_resources if isinstance(raw_resources, PDFDict) else saved_resources
            self._form_stack.add(identity)
            self._execute(decoded_stream_bytes(self.doc, form, label="Form XObject"), resources)
        finally:
            self._form_stack.discard(identity)
            restored = self.stack.pop(); self.state = restored; surface.clip[:] = restored.clip; text.ctm = restored.ctm
            self.resources = saved_resources

    def _inline_image(self, inline: InlineImage) -> None:
        _, state, _ = self._require()
        stream = PDFStream(_inline_dictionary(inline.dictionary), inline.data)
        try: image = decode_image(self.doc, stream, resources=self.resources)
        except ImageError as exc: raise UnsupportedRenderingError(str(exc)) from exc
        self._draw_image(image, state.ctm)

    def _draw_image(self, image: DecodedImage, matrix: Matrix) -> None:
        surface, state, _ = self._require()
        determinant = matrix.a * matrix.d - matrix.b * matrix.c
        if abs(determinant) < 1e-12: return
        corners = [matrix.transform(0,0), matrix.transform(1,0), matrix.transform(1,1), matrix.transform(0,1)]
        min_x = max(0, math.floor(min(point[0] for point in corners)))
        max_x = min(surface.width - 1, math.ceil(max(point[0] for point in corners)))
        min_y = max(0, math.floor(min(point[1] for point in corners)))
        max_y = min(surface.height - 1, math.ceil(max(point[1] for point in corners)))
        inv_a = matrix.d / determinant; inv_b = -matrix.b / determinant
        inv_c = -matrix.c / determinant; inv_d = matrix.a / determinant
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                dx, dy = x + 0.5 - matrix.e, y + 0.5 - matrix.f
                u = inv_a * dx + inv_c * dy
                v = inv_b * dx + inv_d * dy
                if not (0 <= u <= 1 and 0 <= v <= 1): continue
                sx = min(image.width - 1, max(0, int(u * image.width)))
                sy = min(image.height - 1, max(0, int((1.0 - v) * image.height)))
                color = image.pixel(sx, sy)
                effective = Color(color.r, color.g, color.b, color.a * state.fill_alpha)
                surface.composite_pixel(x, y, effective, blend_mode=state.blend_mode)

    def _show_text(self, op: str, args: list[PDFObject]) -> None:
        _, state, text = self._require()
        text.ctm = state.ctm
        style = TextPaintStyle(
            fill=Color(state.fill_color.r, state.fill_color.g, state.fill_color.b, state.fill_alpha),
            stroke=Color(state.stroke_color.r, state.stroke_color.g, state.stroke_color.b, state.stroke_alpha),
            line_width=state.line_width,
            blend_mode=state.blend_mode,
        )
        try:
            if op == "Tj":
                if len(args) != 1 or not isinstance(args[0], bytes): raise RenderingError("Tj expects one string")
                text.show(args[0], style)
            elif op == "TJ":
                if len(args) != 1 or not isinstance(args[0], list): raise RenderingError("TJ expects one array")
                text.show_array(args[0], style)
            elif op == "'":
                if len(args) != 1 or not isinstance(args[0], bytes): raise RenderingError("' expects one string")
                text.quote(args[0], style)
            else:
                if len(args) != 3 or not isinstance(args[2], bytes): raise RenderingError('" expects word/char spacing and string')
                text.double_quote(_number(args[0], '"'), _number(args[1], '"'), args[2], style)
        except TextRenderError as exc:
            raise RenderingError(str(exc)) from exc


def render_page(
    source: str | FSPath | bytes | PDFDocument,
    page_number: int = 1,
    *,
    dpi: int = 144,
) -> RenderedPage:
    doc = source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=True)
    if page_number <= 0: raise ValueError("page_number is 1-based")
    for index, page in enumerate(walk_pages(doc), start=1):
        if index == page_number:
            return PageRenderer(doc, dpi=dpi).render_page(page)
    raise IndexError(f"PDF has fewer than {page_number} pages")
