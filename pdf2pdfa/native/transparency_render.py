"""Owned transparency-group and soft-mask compositor.

This layer extends the canonical page renderer with isolated transparency Form
XObjects and ExtGState soft masks. Non-isolated and knockout groups remain
explicitly unsupported until their backdrop/shape semantics are implemented;
that is preferable to flattening them incorrectly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path as FSPath

from .affine_stroke import StrokeError, stroke_affine
from .color import ColorSpaceError, parse_color_space
from .document import PDFDocument
from .function import PDFFunction, FunctionError
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import GraphicsState, RenderedPage, RenderingError, UnsupportedRenderingError
from .raster import Color, Matrix, Path, Surface, rasterize_fill
from .render import OwnedPageRenderer
from .structure import decoded_stream_bytes, resolve, walk_pages
from .text_render import TrueTypeTextRenderer


def _number(value: PDFObject, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise RenderingError(f"{label} expects a number")
    return float(value)


def _integer(value: PDFObject, label: str) -> int:
    number = _number(value, label)
    integer = int(number)
    if integer != number:
        raise RenderingError(f"{label} expects an integer")
    return integer


def _name(value: PDFObject | None, label: str) -> str:
    if not isinstance(value, PDFName):
        raise RenderingError(f"{label} expects a name")
    return value.value


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _matrix(doc: PDFDocument, form: PDFStream) -> Matrix:
    value = resolve(doc, form.get("Matrix")) if form.get("Matrix") is not None else None
    if value is None:
        return Matrix()
    if not isinstance(value, list) or len(value) != 6:
        raise RenderingError("Form /Matrix is malformed")
    return Matrix(*(_number(resolve(doc, item), "Form/Matrix") for item in value))


def _bbox(doc: PDFDocument, form: PDFStream) -> tuple[float, float, float, float]:
    value = resolve(doc, form.get("BBox"))
    if not isinstance(value, list) or len(value) != 4:
        raise RenderingError("Form XObject requires BBox")
    numbers = tuple(_number(resolve(doc, item), "Form/BBox") for item in value)
    if numbers[2] < numbers[0] or numbers[3] < numbers[1]:
        raise RenderingError("Form BBox is invalid")
    return numbers  # type: ignore[return-value]


class TransparencyRenderer(OwnedPageRenderer):
    def __init__(self, doc: PDFDocument, *, dpi: int = 144) -> None:
        super().__init__(doc, dpi=dpi)
        self.soft_mask: bytearray | None = None
        self._soft_stack: list[bytearray | None] = []
        self._group_depth = 0

    def render_page(self, page):
        self.soft_mask = None
        self._soft_stack.clear()
        self._group_depth = 0
        return super().render_page(page)

    def _instruction(self, instruction):
        if instruction.operator == "q":
            self._soft_stack.append(
                bytearray(self.soft_mask) if self.soft_mask is not None else None
            )
            return super()._instruction(instruction)
        if instruction.operator == "Q":
            result = super()._instruction(instruction)
            if not self._soft_stack:
                raise RenderingError("soft-mask graphics stack underflow")
            self.soft_mask = self._soft_stack.pop()
            return result
        return super()._instruction(instruction)

    def _masked_clip(self) -> bytearray | None:
        surface, _, _ = self._require()
        if self.soft_mask is None:
            return None
        if len(self.soft_mask) != surface.width * surface.height:
            raise RenderingError("soft-mask raster dimensions do not match page")
        original = bytearray(surface.clip)
        for index, mask in enumerate(self.soft_mask):
            surface.clip[index] = (surface.clip[index] * mask + 127) // 255
        return original

    def _restore_clip(self, original: bytearray | None) -> None:
        if original is not None:
            surface, _, _ = self._require()
            surface.clip[:] = original

    def _paint_operator(self, op: str, args: list[PDFObject]) -> None:
        if args:
            raise RenderingError(f"{op} takes no operands")
        surface, state, _ = self._require()
        close = op in {"s", "b", "b*"}
        if close and self.path.subpaths:
            self.path.close()
        fill = op in {"f", "F", "f*", "B", "B*", "b", "b*"}
        stroke = op in {"S", "s", "B", "B*", "b", "b*"}
        even_odd = op in {"f*", "B*", "b*"}

        original_clip = self._masked_clip()
        try:
            if fill:
                surface.fill_path(
                    self.path,
                    Color(
                        state.fill_color.r,
                        state.fill_color.g,
                        state.fill_color.b,
                        state.fill_alpha,
                    ),
                    even_odd=even_odd,
                    blend_mode=state.blend_mode,
                )
            if stroke:
                path_ctm = self._path_ctm or state.ctm
                if self._mixed_path_ctm or path_ctm != state.ctm:
                    raise UnsupportedRenderingError(
                        "stroke path constructed under multiple/different CTMs"
                    )
                try:
                    stroke_affine(
                        surface,
                        self.path,
                        path_ctm=path_ctm,
                        line_width=state.line_width,
                        line_cap=state.line_cap,
                        line_join=state.line_join,
                        miter_limit=state.miter_limit,
                        dash_array=state.dash_array,
                        dash_phase=state.dash_phase,
                        color=Color(
                            state.stroke_color.r,
                            state.stroke_color.g,
                            state.stroke_color.b,
                            state.stroke_alpha,
                        ),
                        blend_mode=state.blend_mode,
                    )
                except StrokeError as exc:
                    raise RenderingError(str(exc)) from exc
        finally:
            self._restore_clip(original_clip)

        self._apply_pending_clip()
        self.path.clear()
        self.pending_clip = None
        self._path_ctm = None
        self._mixed_path_ctm = False

    def _show_text(self, op: str, args: list[PDFObject]) -> None:
        original = self._masked_clip()
        try:
            super()._show_text(op, args)
        finally:
            self._restore_clip(original)

    def _draw_image(self, image, matrix: Matrix) -> None:
        original = self._masked_clip()
        try:
            super()._draw_image(image, matrix)
        finally:
            self._restore_clip(original)

    def _extgstate(self, name: str) -> None:
        value = self._resolve_extgstate(name)
        _, state, _ = self._require()
        for key, raw_value in value.items():
            raw = resolve(self.doc, raw_value)
            if key == "ca":
                state.fill_alpha = _clamp(_number(raw, "ExtGState/ca"))
            elif key == "CA":
                state.stroke_alpha = _clamp(_number(raw, "ExtGState/CA"))
            elif key == "BM":
                candidate = raw[0] if isinstance(raw, list) and raw else raw
                mode = _name(candidate, "ExtGState/BM")
                from .page_render import _BLEND_MODES
                if mode not in _BLEND_MODES:
                    raise UnsupportedRenderingError(f"unsupported blend mode /{mode}")
                state.blend_mode = mode
            elif key == "SMask":
                if isinstance(raw, PDFName) and raw.value == "None":
                    self.soft_mask = None
                elif isinstance(raw, PDFDict):
                    self.soft_mask = self._build_soft_mask(raw)
                else:
                    raise RenderingError("ExtGState/SMask is malformed")
            elif key == "LW":
                state.line_width = max(0.0, _number(raw, "ExtGState/LW"))
            elif key == "LC":
                state.line_cap = _integer(raw, "ExtGState/LC")
            elif key == "LJ":
                state.line_join = _integer(raw, "ExtGState/LJ")
            elif key == "ML":
                state.miter_limit = max(1.0, _number(raw, "ExtGState/ML"))
            elif key == "D":
                if not isinstance(raw, list) or len(raw) != 2:
                    raise RenderingError("ExtGState/D is malformed")
                dash = resolve(self.doc, raw[0])
                phase = resolve(self.doc, raw[1])
                if not isinstance(dash, list):
                    raise RenderingError("ExtGState/D dash array is malformed")
                state.dash_array = tuple(
                    _number(resolve(self.doc, item), "ExtGState/D") for item in dash
                )
                state.dash_phase = _number(phase, "ExtGState/D")
            elif key == "RI":
                state.rendering_intent = _name(raw, "ExtGState/RI")
            elif key == "FL":
                state.flatness = max(0.0, min(100.0, _number(raw, "ExtGState/FL")))
            elif key in {"Type", "AIS", "TK", "OP", "op", "OPM"}:
                continue
            elif key in {"TR", "TR2", "HT", "BG", "BG2", "UCR", "UCR2", "Font"}:
                if key == "TR2" and isinstance(raw, PDFName) and raw.value == "Default":
                    continue
                raise UnsupportedRenderingError(
                    f"ExtGState /{key} requires dedicated owned support"
                )
            else:
                raise UnsupportedRenderingError(f"unknown ExtGState key /{key}")

    def _resolve_extgstate(self, name: str) -> PDFDict:
        if self.resources is None or self.resources.get("ExtGState") is None:
            raise RenderingError("resources have no ExtGState dictionary")
        table = resolve(self.doc, self.resources.get("ExtGState"))
        if not isinstance(table, PDFDict) or name not in table:
            raise RenderingError(f"ExtGState resource /{name} not found")
        value = resolve(self.doc, table[name])
        if not isinstance(value, PDFDict):
            raise RenderingError("ExtGState resource is not a dictionary")
        return value

    def _form(self, form: PDFStream) -> None:
        group = resolve(self.doc, form.get("Group")) if form.get("Group") is not None else None
        if isinstance(group, PDFDict):
            subtype = resolve(self.doc, group.get("S"))
            if isinstance(subtype, PDFName) and subtype.value == "Transparency":
                self._paint_transparency_group(form, group)
                return
        super()._form(form)

    def _group_flags(self, group: PDFDict) -> tuple[bool, bool]:
        isolated = bool(resolve(self.doc, group.get("I"))) if group.get("I") is not None else False
        knockout = bool(resolve(self.doc, group.get("K"))) if group.get("K") is not None else False
        return isolated, knockout

    def _paint_transparency_group(self, form: PDFStream, group: PDFDict) -> None:
        surface, state, _ = self._require()
        isolated, knockout = self._group_flags(group)
        if knockout:
            raise UnsupportedRenderingError(
                "knockout transparency groups require owned knockout shape compositor"
            )
        if not isolated:
            raise UnsupportedRenderingError(
                "non-isolated transparency groups require owned backdrop compositor"
            )
        caller_alpha = state.fill_alpha
        caller_blend = state.blend_mode
        caller_mask = bytearray(self.soft_mask) if self.soft_mask is not None else None
        group_surface = self._render_isolated_group(form)
        for y in range(surface.height):
            row = y * surface.width
            for x in range(surface.width):
                index = row + x
                source = group_surface.get_pixel(x, y)
                if source.a <= 0:
                    continue
                alpha = source.a * caller_alpha
                if caller_mask is not None:
                    alpha *= caller_mask[index] / 255.0
                if alpha <= 0:
                    continue
                surface.composite_pixel(
                    x,
                    y,
                    Color(source.r, source.g, source.b, alpha),
                    blend_mode=caller_blend,
                )

    def _render_isolated_group(self, form: PDFStream) -> Surface:
        parent_surface, parent_state, parent_text = self._require()
        if self._group_depth >= 32:
            raise RenderingError("transparency group nesting exceeds 32")
        form_matrix = _matrix(self.doc, form)
        bbox = _bbox(self.doc, form)
        group_surface = Surface(
            parent_surface.width,
            parent_surface.height,
            background=Color(0, 0, 0, 0),
        )
        group_surface.clip[:] = parent_surface.clip
        inner_state = parent_state.clone()
        inner_state.ctm = parent_state.ctm.concat(form_matrix)
        inner_state.fill_alpha = 1.0
        inner_state.stroke_alpha = 1.0
        inner_state.blend_mode = "Normal"
        inner_state.clip[:] = group_surface.clip

        clip_path = Path()
        corners = [
            inner_state.ctm.transform(bbox[0], bbox[1]),
            inner_state.ctm.transform(bbox[2], bbox[1]),
            inner_state.ctm.transform(bbox[2], bbox[3]),
            inner_state.ctm.transform(bbox[0], bbox[3]),
        ]
        clip_path.move_to(*corners[0])
        for point in corners[1:]:
            clip_path.line_to(*point)
        clip_path.close()
        group_surface.apply_clip_mask(
            rasterize_fill(clip_path, group_surface.width, group_surface.height)
        )
        inner_state.clip[:] = group_surface.clip

        raw_resources = resolve(self.doc, form.get("Resources")) if form.get("Resources") is not None else self.resources
        resources = raw_resources if isinstance(raw_resources, PDFDict) else self.resources

        saved_surface = self.surface
        saved_state = self.state
        saved_text = self.text
        saved_resources = self.resources
        saved_path = self.path
        saved_pending = self.pending_clip
        saved_path_ctm = self._path_ctm
        saved_mixed = self._mixed_path_ctm
        saved_soft = self.soft_mask
        saved_soft_stack = self._soft_stack
        saved_stack = self.stack
        self._group_depth += 1
        try:
            self.surface = group_surface
            self.state = inner_state
            self.text = TrueTypeTextRenderer(group_surface, ctm=inner_state.ctm)
            self.resources = resources
            self.path = Path()
            self.pending_clip = None
            self._path_ctm = None
            self._mixed_path_ctm = False
            self.soft_mask = None
            self._soft_stack = []
            self.stack = []
            self._execute(decoded_stream_bytes(self.doc, form, label="transparency group Form"), resources)
            if self.stack:
                raise RenderingError("transparency group ended with unbalanced q/Q")
            if self.text.in_text_object:
                raise RenderingError("transparency group ended inside BT/ET")
        finally:
            self._group_depth -= 1
            self.surface = saved_surface
            self.state = saved_state
            self.text = saved_text
            self.resources = saved_resources
            self.path = saved_path
            self.pending_clip = saved_pending
            self._path_ctm = saved_path_ctm
            self._mixed_path_ctm = saved_mixed
            self.soft_mask = saved_soft
            self._soft_stack = saved_soft_stack
            self.stack = saved_stack
        return group_surface

    def _build_soft_mask(self, mask: PDFDict) -> bytearray:
        kind_value = resolve(self.doc, mask.get("S"))
        kind = _name(kind_value, "SMask/S")
        if kind not in {"Alpha", "Luminosity"}:
            raise UnsupportedRenderingError(f"unsupported soft-mask subtype /{kind}")
        group_value = resolve(self.doc, mask.get("G"))
        if not isinstance(group_value, PDFStream):
            raise RenderingError("SMask/G is not a Form XObject stream")
        group_dict = resolve(self.doc, group_value.get("Group")) if group_value.get("Group") is not None else None
        if not isinstance(group_dict, PDFDict):
            raise RenderingError("SMask/G Form lacks transparency Group dictionary")
        group_subtype = resolve(self.doc, group_dict.get("S"))
        if not isinstance(group_subtype, PDFName) or group_subtype.value != "Transparency":
            raise RenderingError("SMask/G group is not /Transparency")
        _, knockout = self._group_flags(group_dict)
        if knockout:
            raise UnsupportedRenderingError("knockout soft-mask groups are not supported yet")
        # Soft-mask groups are evaluated as isolated mask sources; /BC supplies
        # the luminosity backdrop when needed.
        group_surface = self._render_isolated_group(group_value)
        surface, _, _ = self._require()
        result = bytearray(surface.width * surface.height)

        backdrop_rgb = (0.0, 0.0, 0.0)
        if kind == "Luminosity" and mask.get("BC") is not None:
            raw_bc = resolve(self.doc, mask.get("BC"))
            if not isinstance(raw_bc, list):
                raise RenderingError("SMask/BC is not an array")
            group_cs_value = group_dict.get("CS")
            if group_cs_value is None:
                if len(raw_bc) == 1:
                    group_cs_value = PDFName("DeviceGray")
                elif len(raw_bc) == 3:
                    group_cs_value = PDFName("DeviceRGB")
                elif len(raw_bc) == 4:
                    group_cs_value = PDFName("DeviceCMYK")
                else:
                    raise RenderingError("cannot infer soft-mask BC color space")
            try:
                group_space = parse_color_space(
                    self.doc, group_cs_value, resources=self.resources
                )
            except ColorSpaceError as exc:
                raise UnsupportedRenderingError(str(exc)) from exc
            if len(raw_bc) != group_space.components:
                raise RenderingError("SMask/BC component count does not match group CS")
            backdrop_rgb = group_space.rgb(
                tuple(_number(resolve(self.doc, item), "SMask/BC") for item in raw_bc)
            )

        transfer = None
        raw_tr = resolve(self.doc, mask.get("TR")) if mask.get("TR") is not None else None
        if raw_tr is not None:
            if isinstance(raw_tr, PDFName) and raw_tr.value in {"Identity", "Default"}:
                transfer = None
            else:
                try:
                    transfer = PDFFunction(self.doc, raw_tr)
                except FunctionError as exc:
                    raise UnsupportedRenderingError(str(exc)) from exc

        for y in range(surface.height):
            for x in range(surface.width):
                index = y * surface.width + x
                pixel = group_surface.get_pixel(x, y)
                if kind == "Alpha":
                    value = pixel.a
                else:
                    # Composite the group color over BC before evaluating mask luminosity.
                    r = pixel.r * pixel.a + backdrop_rgb[0] * (1.0 - pixel.a)
                    g = pixel.g * pixel.a + backdrop_rgb[1] * (1.0 - pixel.a)
                    b = pixel.b * pixel.a + backdrop_rgb[2] * (1.0 - pixel.a)
                    value = 0.299 * r + 0.587 * g + 0.114 * b
                if transfer is not None:
                    output = transfer.evaluate([value])
                    if len(output) != 1:
                        raise RenderingError("SMask/TR shall produce one output")
                    value = output[0]
                result[index] = round(_clamp(value) * 255)
        return result


def render_page(
    source: str | FSPath | bytes | PDFDocument,
    page_number: int = 1,
    *,
    dpi: int = 144,
) -> RenderedPage:
    doc = source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=True)
    if page_number <= 0:
        raise ValueError("page_number is 1-based")
    for index, page in enumerate(walk_pages(doc), start=1):
        if index == page_number:
            return TransparencyRenderer(doc, dpi=dpi).render_page(page)
    raise IndexError(f"PDF has fewer than {page_number} pages")
