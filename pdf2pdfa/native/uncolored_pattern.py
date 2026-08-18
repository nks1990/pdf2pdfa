"""Owned PatternType 1 / PaintType 2 uncolored tiling fills.

An uncolored tiling pattern contributes a shape; the color comes from the base
color-space components supplied with ``scn``.  This renderer therefore executes
each cell on a transparent temporary surface, treats its alpha as a shape mask,
and composites the selected base color onto the parent surface exactly once.

The first production-safe scope deliberately accepts only painting operations
whose color is inherited from the graphics state (paths, text, strokes and
ImageMask). Intrinsic-color images, shadings, nested pattern selection and all
color-setting operators inside a PaintType 2 cell fail closed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .affine_stroke import StrokeError, inverse
from .cff_text_render import OwnedOutlineTextRenderer
from .color import ColorSpace, ColorSpaceError, parse_color_space
from .content import ContentInstruction, InlineImage
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError, _name, _number, _resolve_resource
from .pattern_render import PatternColorSpaceState, PatternRenderError, UnsupportedPatternError, _pattern_matrix
from .raster import Color, Matrix, Path, Surface, rasterize_fill
from .structure import decoded_stream_bytes, resolve
from .tiling_pattern import (
    MAX_PATTERN_DEPTH,
    MAX_PATTERN_TILES,
    _clip_device_bounds,
    _exact_integer,
    _numbers,
    _pattern_resources,
    _tile_bbox_path,
    _tile_range,
    _validate_cell_program,
)


@dataclass(frozen=True, slots=True)
class _BaseSelection:
    space: ColorSpace
    rgb: tuple[float, float, float]


_COLOR_OPERATORS = {
    "G", "g", "RG", "rg", "K", "k", "CS", "cs", "SC", "sc", "SCN", "scn"
}


def _pattern_base_space(doc, resources: PDFDict | None, operand: PDFObject) -> ColorSpace | None:
    name = _name(operand, "CS/cs")
    if name == "Pattern":
        return None
    try:
        value = _resolve_resource(doc, resources, "ColorSpace", name)
    except RenderingError:
        return None
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != 2:
        return None
    family = resolve(doc, value[0])
    if not isinstance(family, PDFName) or family.value != "Pattern":
        return None
    try:
        return parse_color_space(doc, value[1], resources=resources)
    except ColorSpaceError as exc:
        raise PatternRenderError(f"uncolored Pattern base color space is invalid: {exc}") from exc


def _image_mask(doc, stream: PDFStream) -> bool:
    raw = resolve(doc, stream.get("ImageMask")) if stream.get("ImageMask") is not None else False
    if not isinstance(raw, bool):
        raise PatternRenderError("Image /ImageMask shall be boolean")
    return raw


class UncoloredTilingPatternRendererMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        self._fill_uncolored_space: ColorSpace | None = None
        self._stroke_uncolored_space: ColorSpace | None = None
        self._fill_uncolored_selection: _BaseSelection | None = None
        self._stroke_uncolored_selection: _BaseSelection | None = None
        self._uncolored_stack: list[
            tuple[
                ColorSpace | None,
                ColorSpace | None,
                _BaseSelection | None,
                _BaseSelection | None,
            ]
        ] = []
        self._uncolored_pattern_depth = 0

    def _instruction(self, instruction: ContentInstruction) -> None:
        op = instruction.operator
        args = list(instruction.operands)

        if self._uncolored_pattern_depth:
            if op in _COLOR_OPERATORS:
                raise UnsupportedPatternError(
                    f"PaintType 2 cell shall not set/select color with operator {op}"
                )
            if op == "sh":
                raise UnsupportedPatternError(
                    "PaintType 2 cell shall not paint an intrinsically colored shading"
                )
            if op == "Do" and len(args) == 1:
                value = _resolve_resource(
                    self.doc, self.resources, "XObject", _name(args[0], "Do")  # type: ignore[attr-defined]
                )
                resolved = resolve(self.doc, value)  # type: ignore[attr-defined]
                if isinstance(resolved, PDFStream):
                    subtype = resolve(self.doc, resolved.get("Subtype"))
                    if isinstance(subtype, PDFName) and subtype.value == "Image" and not _image_mask(self.doc, resolved):  # type: ignore[attr-defined]
                        raise UnsupportedPatternError(
                            "PaintType 2 cell cannot paint an intrinsic-color Image XObject"
                        )
            return super()._instruction(instruction)  # type: ignore[misc]

        if op == "q":
            self._uncolored_stack.append(
                (
                    self._fill_uncolored_space,
                    self._stroke_uncolored_space,
                    self._fill_uncolored_selection,
                    self._stroke_uncolored_selection,
                )
            )
            return super()._instruction(instruction)  # type: ignore[misc]

        if op == "Q":
            result = super()._instruction(instruction)  # type: ignore[misc]
            if not self._uncolored_stack:
                raise RenderingError("uncolored-pattern graphics-state stack underflow")
            (
                self._fill_uncolored_space,
                self._stroke_uncolored_space,
                self._fill_uncolored_selection,
                self._stroke_uncolored_selection,
            ) = self._uncolored_stack.pop()
            return result

        if op in {"cs", "CS"} and len(args) == 1:
            base = _pattern_base_space(self.doc, self.resources, args[0])  # type: ignore[attr-defined]
            result = super()._instruction(instruction)  # type: ignore[misc]
            if op == "cs":
                self._fill_uncolored_space = base
                self._fill_uncolored_selection = None
            else:
                self._stroke_uncolored_space = base
                self._stroke_uncolored_selection = None
            return result

        if op in {"scn", "SCN"}:
            stroking = op == "SCN"
            base = self._stroke_uncolored_space if stroking else self._fill_uncolored_space
            if base is not None:
                result = super()._instruction(instruction)  # type: ignore[misc]
                if len(args) != base.components + 1:
                    raise PatternRenderError(
                        f"uncolored Pattern {op} expects {base.components} base component(s) plus pattern name"
                    )
                values = tuple(
                    _number(resolve(self.doc, item), f"uncolored Pattern {op}")  # type: ignore[attr-defined]
                    for item in args[:-1]
                )
                try:
                    rgb = base.rgb(values)
                except ColorSpaceError as exc:
                    raise PatternRenderError(str(exc)) from exc
                selection = _BaseSelection(base, rgb)
                if stroking:
                    self._stroke_uncolored_selection = selection
                else:
                    self._fill_uncolored_selection = selection
                return result

        if op in {"g", "rg", "k"}:
            self._fill_uncolored_space = None
            self._fill_uncolored_selection = None
        elif op in {"G", "RG", "K"}:
            self._stroke_uncolored_space = None
            self._stroke_uncolored_selection = None

        return super()._instruction(instruction)  # type: ignore[misc]

    def _inline_image(self, inline: InlineImage) -> None:
        if self._uncolored_pattern_depth:
            raw = inline.dictionary.get("IM", inline.dictionary.get("ImageMask", False))
            if not isinstance(raw, bool) or not raw:
                raise UnsupportedPatternError(
                    "PaintType 2 cell cannot paint an intrinsic-color inline image"
                )
        return super()._inline_image(inline)  # type: ignore[misc]

    def _paint_pattern_fill(self, *, even_odd: bool) -> None:
        selection = self._fill_pattern  # type: ignore[attr-defined]
        if (
            selection is None
            or selection.pattern_type != 1
            or not self._fill_pattern_space.has_base_space  # type: ignore[attr-defined]
        ):
            return super()._paint_pattern_fill(even_odd=even_odd)  # type: ignore[misc]
        base_selection = self._fill_uncolored_selection
        if self._fill_uncolored_space is None or base_selection is None:
            raise PatternRenderError(
                "uncolored Pattern fill requires base color components before the pattern name"
            )
        if self._uncolored_pattern_depth >= MAX_PATTERN_DEPTH:
            raise PatternRenderError(
                f"uncolored tiling pattern recursion exceeds {MAX_PATTERN_DEPTH}"
            )

        resolved = resolve(self.doc, selection.value)  # type: ignore[attr-defined]
        if not isinstance(resolved, PDFStream):
            raise PatternRenderError("PatternType 1 shall be a content stream")
        dictionary = resolved.dictionary
        paint_type = _exact_integer(
            self.doc, dictionary.get("PaintType"), "Pattern/PaintType"  # type: ignore[attr-defined]
        )
        if paint_type != 2:
            if paint_type == 1:
                return super()._paint_pattern_fill(even_odd=even_odd)  # type: ignore[misc]
            raise PatternRenderError(f"invalid tiling Pattern/PaintType {paint_type}")
        tiling_type = _exact_integer(
            self.doc, dictionary.get("TilingType"), "Pattern/TilingType"  # type: ignore[attr-defined]
        )
        if tiling_type not in (1, 2, 3):
            raise PatternRenderError(f"invalid tiling Pattern/TilingType {tiling_type}")

        bbox = _numbers(self.doc, dictionary.get("BBox"), 4, "Pattern/BBox")  # type: ignore[attr-defined]
        if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
            raise PatternRenderError("tiling Pattern/BBox is invalid")
        x_step = _number(resolve(self.doc, dictionary.get("XStep")), "Pattern/XStep")  # type: ignore[attr-defined]
        y_step = _number(resolve(self.doc, dictionary.get("YStep")), "Pattern/YStep")  # type: ignore[attr-defined]
        if abs(x_step) <= 1e-15 or abs(y_step) <= 1e-15:
            raise PatternRenderError("tiling pattern XStep/YStep shall be non-zero")

        resources = _pattern_resources(self.doc, dictionary)  # type: ignore[attr-defined]
        content = decoded_stream_bytes(
            self.doc, resolved, label=f"uncolored tiling pattern /{selection.name}"  # type: ignore[attr-defined]
        )
        _validate_cell_program(content)

        parent_surface, state, text = self._require()  # type: ignore[attr-defined]
        parent_ctm = state.ctm
        fill_mask = rasterize_fill(
            self.path,  # type: ignore[attr-defined]
            parent_surface.width,
            parent_surface.height,
            even_odd=even_odd,
        )
        parent_clip = bytearray(parent_surface.clip)
        pattern_clip = bytearray(
            (parent_clip[index] * fill_mask[index] + 127) // 255
            for index in range(len(parent_clip))
        )
        device_bounds = _clip_device_bounds(
            pattern_clip, parent_surface.width, parent_surface.height
        )
        if device_bounds is None:
            return

        base_ctm = parent_ctm.concat(_pattern_matrix(self.doc, dictionary))  # type: ignore[attr-defined]
        try:
            inv = inverse(base_ctm)
        except StrokeError as exc:
            raise PatternRenderError("uncolored tiling pattern CTM is singular") from exc
        dx0, dy0, dx1, dy1 = device_bounds
        viewport = [
            inv.transform(dx0, dy0), inv.transform(dx1, dy0),
            inv.transform(dx1, dy1), inv.transform(dx0, dy1),
        ]
        x_indices = _tile_range(
            viewport_min=min(point[0] for point in viewport),
            viewport_max=max(point[0] for point in viewport),
            bbox_min=bbox[0], bbox_max=bbox[2], step=x_step,
        )
        y_indices = _tile_range(
            viewport_min=min(point[1] for point in viewport),
            viewport_max=max(point[1] for point in viewport),
            bbox_min=bbox[1], bbox_max=bbox[3], step=y_step,
        )
        tile_count = len(x_indices) * len(y_indices)
        if tile_count > MAX_PATTERN_TILES:
            raise UnsupportedPatternError(
                f"uncolored tiling pattern would require {tile_count} cells; owned limit is {MAX_PATTERN_TILES}"
            )

        saved_resources = self.resources  # type: ignore[attr-defined]
        saved_path = copy.deepcopy(self.path)  # type: ignore[attr-defined]
        saved_pending_clip = self.pending_clip  # type: ignore[attr-defined]
        saved_path_ctm = getattr(self, "_path_ctm", None)
        saved_mixed_path_ctm = getattr(self, "_mixed_path_ctm", False)
        saved_text = text
        saved_fill_space = self._fill_pattern_space  # type: ignore[attr-defined]
        saved_stroke_space = self._stroke_pattern_space  # type: ignore[attr-defined]
        saved_fill_pattern = self._fill_pattern  # type: ignore[attr-defined]
        saved_stroke_pattern = self._stroke_pattern  # type: ignore[attr-defined]
        saved_uncolored_space = self._fill_uncolored_space
        saved_uncolored_selection = self._fill_uncolored_selection
        caller_alpha = state.fill_alpha
        caller_blend = state.blend_mode
        caller_soft = bytearray(self.soft_mask) if getattr(self, "soft_mask", None) is not None else None

        self._uncolored_pattern_depth += 1
        try:
            for iy in y_indices:
                for ix in x_indices:
                    tile_ctm = base_ctm.concat(
                        Matrix(1, 0, 0, 1, ix * x_step, iy * y_step)
                    )
                    tile_clip = rasterize_fill(
                        _tile_bbox_path(tile_ctm, bbox),
                        parent_surface.width,
                        parent_surface.height,
                    )
                    combined_clip = bytearray(
                        (pattern_clip[index] * tile_clip[index] + 127) // 255
                        for index in range(len(pattern_clip))
                    )
                    if not any(combined_clip):
                        continue

                    mask_surface = Surface(
                        parent_surface.width,
                        parent_surface.height,
                        background=Color(0, 0, 0, 0),
                    )
                    mask_surface.clip[:] = combined_clip

                    # Synthetic q/Q protects the caller's graphics state. The
                    # current state itself becomes the cell state until Q restores it.
                    self._instruction(ContentInstruction((), "q", 0, 0))  # type: ignore[attr-defined]
                    synthetic_depth = len(self.stack)  # type: ignore[attr-defined]
                    self.surface = mask_surface  # type: ignore[attr-defined]
                    cell_text = OwnedOutlineTextRenderer(
                        mask_surface,
                        ctm=tile_ctm,
                        type3_painter=self._paint_type3_glyph,  # type: ignore[attr-defined]
                    )
                    self.text = cell_text  # type: ignore[attr-defined]
                    try:
                        _, tile_state, _ = self._require()  # type: ignore[attr-defined]
                        tile_state.ctm = tile_ctm
                        tile_state.fill_color = Color(1, 1, 1, 1)
                        tile_state.stroke_color = Color(1, 1, 1, 1)
                        tile_state.fill_alpha = 1.0
                        tile_state.stroke_alpha = 1.0
                        tile_state.blend_mode = "Normal"
                        tile_state.clip[:] = combined_clip
                        self.resources = resources  # type: ignore[attr-defined]
                        self.path.clear()  # type: ignore[attr-defined]
                        self.pending_clip = None  # type: ignore[attr-defined]
                        if hasattr(self, "_path_ctm"):
                            self._path_ctm = None
                        if hasattr(self, "_mixed_path_ctm"):
                            self._mixed_path_ctm = False
                        self._fill_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                        self._stroke_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                        self._fill_pattern = None  # type: ignore[attr-defined]
                        self._stroke_pattern = None  # type: ignore[attr-defined]
                        self._fill_uncolored_space = None
                        self._fill_uncolored_selection = None

                        self._execute(content, resources)  # type: ignore[attr-defined]
                        if len(self.stack) != synthetic_depth:  # type: ignore[attr-defined]
                            raise PatternRenderError(
                                f"uncolored tiling pattern /{selection.name} changed graphics-stack depth"
                            )
                        if cell_text.in_text_object:
                            raise PatternRenderError(
                                f"uncolored tiling pattern /{selection.name} left a text object open"
                            )
                    finally:
                        self.surface = parent_surface  # type: ignore[attr-defined]
                        self.text = saved_text  # type: ignore[attr-defined]
                        if len(self.stack) == synthetic_depth:  # type: ignore[attr-defined]
                            self._instruction(ContentInstruction((), "Q", 0, 0))  # type: ignore[attr-defined]
                        elif len(self.stack) < synthetic_depth:  # type: ignore[attr-defined]
                            raise PatternRenderError(
                                f"uncolored tiling pattern /{selection.name} escaped its graphics-state frame"
                            )
                        else:
                            raise PatternRenderError(
                                f"uncolored tiling pattern /{selection.name} left graphics frames open"
                            )
                        self.resources = saved_resources  # type: ignore[attr-defined]
                        self.path = copy.deepcopy(saved_path)  # type: ignore[attr-defined]
                        self.pending_clip = saved_pending_clip  # type: ignore[attr-defined]
                        if hasattr(self, "_path_ctm"):
                            self._path_ctm = saved_path_ctm
                        if hasattr(self, "_mixed_path_ctm"):
                            self._mixed_path_ctm = saved_mixed_path_ctm
                        self._fill_pattern_space = saved_fill_space  # type: ignore[attr-defined]
                        self._stroke_pattern_space = saved_stroke_space  # type: ignore[attr-defined]
                        self._fill_pattern = saved_fill_pattern  # type: ignore[attr-defined]
                        self._stroke_pattern = saved_stroke_pattern  # type: ignore[attr-defined]
                        self._fill_uncolored_space = saved_uncolored_space
                        self._fill_uncolored_selection = saved_uncolored_selection

                    # Convert cell alpha to the externally selected base color.
                    rgb = base_selection.rgb
                    for index in range(parent_surface.width * parent_surface.height):
                        alpha = mask_surface.pixels[index * 4 + 3]
                        if not alpha:
                            continue
                        coverage = alpha / 255.0
                        if caller_soft is not None:
                            coverage *= caller_soft[index] / 255.0
                        y, x = divmod(index, parent_surface.width)
                        parent_surface.composite_pixel(
                            x,
                            y,
                            Color(rgb[0], rgb[1], rgb[2], caller_alpha),
                            coverage=coverage,
                            blend_mode=caller_blend,
                        )
        finally:
            self._uncolored_pattern_depth -= 1
            self.surface = parent_surface  # type: ignore[attr-defined]
            self.text = saved_text  # type: ignore[attr-defined]
            parent_surface.clip[:] = parent_clip
            self.resources = saved_resources  # type: ignore[attr-defined]
            self.path = saved_path  # type: ignore[attr-defined]
            self.pending_clip = saved_pending_clip  # type: ignore[attr-defined]
            if hasattr(self, "_path_ctm"):
                self._path_ctm = saved_path_ctm
            if hasattr(self, "_mixed_path_ctm"):
                self._mixed_path_ctm = saved_mixed_path_ctm
            self._fill_pattern_space = saved_fill_space  # type: ignore[attr-defined]
            self._stroke_pattern_space = saved_stroke_space  # type: ignore[attr-defined]
            self._fill_pattern = saved_fill_pattern  # type: ignore[attr-defined]
            self._stroke_pattern = saved_stroke_pattern  # type: ignore[attr-defined]
            self._fill_uncolored_space = saved_uncolored_space
            self._fill_uncolored_selection = saved_uncolored_selection

        self._apply_pending_clip()  # type: ignore[attr-defined]
        self.path.clear()  # type: ignore[attr-defined]
        self.pending_clip = None  # type: ignore[attr-defined]
        if hasattr(self, "_path_ctm"):
            self._path_ctm = None
        if hasattr(self, "_mixed_path_ctm"):
            self._mixed_path_ctm = False
