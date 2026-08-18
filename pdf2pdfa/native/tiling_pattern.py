"""Owned colored tiling-pattern (PatternType 1 / PaintType 1) renderer.

A tiling pattern cell is a PDF content stream with its own resources and BBox.
The renderer repeats that cell over the pattern lattice, clipping each
invocation to the cell BBox and the path being filled.  Every tile is executed
through the same owned page interpreter, so paths, TrueType/Type3 text,
images, shadings, nested patterns and supported transparency share one source
of truth.

Uncolored PaintType 2 is intentionally separate because its painting operators
must inherit the base color components supplied by ``scn`` while forbidding
cell-local color selection.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import math

from .affine_stroke import StrokeError, inverse
from .content import ContentInstruction, InlineImage, parse_content_stream
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import RenderingError, UnsupportedRenderingError, _number
from .pattern_render import (
    PatternColorSpaceState,
    PatternRenderError,
    UnsupportedPatternError,
    _pattern_dictionary,
    _pattern_matrix,
)
from .raster import Matrix, Path, rasterize_fill
from .structure import decoded_stream_bytes, resolve


MAX_PATTERN_TILES = 50_000
MAX_PATTERN_DEPTH = 16


def _exact_integer(doc, value: PDFObject | None, label: str) -> int:
    if value is None:
        raise PatternRenderError(f"{label} is missing")
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PatternRenderError(f"{label} shall be an integer")
    integer = int(value)
    if integer != value:
        raise PatternRenderError(f"{label} shall be an integer")
    return integer


def _numbers(doc, value: PDFObject | None, count: int, label: str) -> tuple[float, ...]:
    if value is None:
        raise PatternRenderError(f"{label} is missing")
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != count:
        raise PatternRenderError(f"{label} shall contain {count} numbers")
    return tuple(_number(resolve(doc, item), label) for item in value)


def _pattern_resources(doc, dictionary: PDFDict) -> PDFDict:
    raw = dictionary.get("Resources")
    if raw is None:
        return PDFDict()
    raw = resolve(doc, raw)
    if not isinstance(raw, PDFDict):
        raise PatternRenderError("tiling pattern /Resources is not a dictionary")
    return raw


def _validate_cell_program(content: bytes) -> None:
    try:
        instructions = list(parse_content_stream(content))
    except Exception as exc:
        raise PatternRenderError(f"tiling pattern content is invalid: {exc}") from exc
    q_depth = 0
    text_depth = 0
    for item in instructions:
        if isinstance(item, InlineImage):
            continue
        if item.operator == "q":
            q_depth += 1
        elif item.operator == "Q":
            q_depth -= 1
            if q_depth < 0:
                raise PatternRenderError("tiling pattern has Q without matching q")
        elif item.operator == "BT":
            text_depth += 1
            if text_depth != 1:
                raise PatternRenderError("tiling pattern contains nested BT")
        elif item.operator == "ET":
            text_depth -= 1
            if text_depth < 0:
                raise PatternRenderError("tiling pattern has ET without matching BT")
    if q_depth:
        raise PatternRenderError("tiling pattern has unbalanced q/Q")
    if text_depth:
        raise PatternRenderError("tiling pattern has unbalanced BT/ET")


def _tile_range(
    *,
    viewport_min: float,
    viewport_max: float,
    bbox_min: float,
    bbox_max: float,
    step: float,
) -> range:
    spacing = abs(step)
    if spacing <= 1e-15:
        raise PatternRenderError("tiling pattern step shall be non-zero")
    start = math.ceil((viewport_min - bbox_max) / spacing)
    stop = math.floor((viewport_max - bbox_min) / spacing)
    if stop < start:
        return range(0)
    return range(start, stop + 1)


def _tile_bbox_path(matrix: Matrix, bbox: tuple[float, float, float, float]) -> Path:
    x0, y0, x1, y1 = bbox
    points = [
        matrix.transform(x0, y0),
        matrix.transform(x1, y0),
        matrix.transform(x1, y1),
        matrix.transform(x0, y1),
    ]
    path = Path()
    path.move_to(*points[0])
    for point in points[1:]:
        path.line_to(*point)
    path.close()
    return path


class ColoredTilingPatternRendererMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        self._tiling_pattern_depth = 0

    def _paint_pattern_fill(self, *, even_odd: bool) -> None:
        selection = self._fill_pattern  # type: ignore[attr-defined]
        if selection is None or selection.pattern_type != 1:
            return super()._paint_pattern_fill(even_odd=even_odd)  # type: ignore[misc]
        if self._fill_pattern_space.has_base_space:  # type: ignore[attr-defined]
            raise UnsupportedPatternError(
                "uncolored PaintType 2 tiling patterns require owned base-color propagation"
            )
        if self._tiling_pattern_depth >= MAX_PATTERN_DEPTH:
            raise PatternRenderError(
                f"tiling pattern recursion exceeds {MAX_PATTERN_DEPTH}"
            )

        resolved = resolve(self.doc, selection.value)  # type: ignore[attr-defined]
        if not isinstance(resolved, PDFStream):
            raise PatternRenderError("PatternType 1 shall be a content stream")
        dictionary = resolved.dictionary
        paint_type = _exact_integer(self.doc, dictionary.get("PaintType"), "Pattern/PaintType")  # type: ignore[attr-defined]
        if paint_type != 1:
            if paint_type == 2:
                raise UnsupportedPatternError(
                    "uncolored PaintType 2 tiling patterns are not yet implemented"
                )
            raise PatternRenderError(f"invalid tiling Pattern/PaintType {paint_type}")
        tiling_type = _exact_integer(self.doc, dictionary.get("TilingType"), "Pattern/TilingType")  # type: ignore[attr-defined]
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
            self.doc,  # type: ignore[attr-defined]
            resolved,
            label=f"tiling pattern /{selection.name}",
        )
        _validate_cell_program(content)

        surface, state, text = self._require()  # type: ignore[attr-defined]
        fill_mask = rasterize_fill(
            self.path,  # type: ignore[attr-defined]
            surface.width,
            surface.height,
            even_odd=even_odd,
        )
        parent_clip = bytearray(surface.clip)
        pattern_clip = bytearray(
            (parent_clip[index] * fill_mask[index] + 127) // 255
            for index in range(len(parent_clip))
        )

        pattern_matrix = _pattern_matrix(self.doc, dictionary)  # type: ignore[attr-defined]
        base_ctm = state.ctm.concat(pattern_matrix)
        try:
            inv = inverse(base_ctm)
        except StrokeError as exc:
            raise PatternRenderError("tiling pattern CTM is singular") from exc

        viewport = [
            inv.transform(0.0, 0.0),
            inv.transform(float(surface.width), 0.0),
            inv.transform(float(surface.width), float(surface.height)),
            inv.transform(0.0, float(surface.height)),
        ]
        min_x = min(point[0] for point in viewport)
        max_x = max(point[0] for point in viewport)
        min_y = min(point[1] for point in viewport)
        max_y = max(point[1] for point in viewport)
        x_indices = _tile_range(
            viewport_min=min_x,
            viewport_max=max_x,
            bbox_min=bbox[0],
            bbox_max=bbox[2],
            step=x_step,
        )
        y_indices = _tile_range(
            viewport_min=min_y,
            viewport_max=max_y,
            bbox_min=bbox[1],
            bbox_max=bbox[3],
            step=y_step,
        )
        tile_count = len(x_indices) * len(y_indices)
        if tile_count > MAX_PATTERN_TILES:
            raise UnsupportedPatternError(
                f"tiling pattern would require {tile_count} cells; owned limit is {MAX_PATTERN_TILES}"
            )

        saved_resources = self.resources  # type: ignore[attr-defined]
        saved_path = copy.deepcopy(self.path)  # type: ignore[attr-defined]
        saved_pending_clip = self.pending_clip  # type: ignore[attr-defined]
        saved_path_ctm = getattr(self, "_path_ctm", None)
        saved_mixed_path_ctm = getattr(self, "_mixed_path_ctm", False)
        saved_text_state = copy.deepcopy(text.state)
        saved_in_text = text.in_text_object
        saved_fill_space = self._fill_pattern_space  # type: ignore[attr-defined]
        saved_stroke_space = self._stroke_pattern_space  # type: ignore[attr-defined]
        saved_fill_pattern = self._fill_pattern  # type: ignore[attr-defined]
        saved_stroke_pattern = self._stroke_pattern  # type: ignore[attr-defined]
        saved_pattern_stack = list(self._pattern_stack)  # type: ignore[attr-defined]

        self._tiling_pattern_depth += 1
        try:
            for iy in y_indices:
                for ix in x_indices:
                    translation = Matrix(1, 0, 0, 1, ix * abs(x_step), iy * abs(y_step))
                    tile_ctm = base_ctm.concat(translation)
                    tile_mask = rasterize_fill(
                        _tile_bbox_path(tile_ctm, bbox),
                        surface.width,
                        surface.height,
                        even_odd=False,
                    )
                    surface.clip = bytearray(
                        (pattern_clip[index] * tile_mask[index] + 127) // 255
                        for index in range(len(pattern_clip))
                    )
                    if not any(surface.clip):
                        continue

                    # Each cell starts in an isolated graphics-state frame. Use
                    # dynamic dispatch so transparency/pattern stacks are saved
                    # by their respective mixins as well.
                    self._fill_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                    self._stroke_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                    self._fill_pattern = None  # type: ignore[attr-defined]
                    self._stroke_pattern = None  # type: ignore[attr-defined]
                    self._instruction(ContentInstruction((), "q", 0, 0))  # type: ignore[attr-defined]
                    synthetic_depth = len(self.stack)  # type: ignore[attr-defined]
                    try:
                        _, tile_state, tile_text = self._require()  # type: ignore[attr-defined]
                        tile_state.ctm = tile_ctm
                        tile_text.ctm = tile_ctm
                        self.resources = resources  # type: ignore[attr-defined]
                        self.path.clear()  # type: ignore[attr-defined]
                        self.pending_clip = None  # type: ignore[attr-defined]
                        if hasattr(self, "_path_ctm"):
                            self._path_ctm = None
                        if hasattr(self, "_mixed_path_ctm"):
                            self._mixed_path_ctm = False
                        self._execute(content, resources)  # type: ignore[attr-defined]
                        if len(self.stack) != synthetic_depth:  # type: ignore[attr-defined]
                            raise PatternRenderError(
                                f"tiling pattern /{selection.name} changed graphics-stack depth"
                            )
                        if tile_text.in_text_object:
                            raise PatternRenderError(
                                f"tiling pattern /{selection.name} left a text object open"
                            )
                    finally:
                        if len(self.stack) == synthetic_depth:  # type: ignore[attr-defined]
                            self._instruction(ContentInstruction((), "Q", 0, 0))  # type: ignore[attr-defined]
                        elif len(self.stack) < synthetic_depth:  # type: ignore[attr-defined]
                            raise PatternRenderError(
                                f"tiling pattern /{selection.name} escaped its graphics-state frame"
                            )
                        else:
                            # Cell q/Q balance was prevalidated, so this path is
                            # an interpreter invariant failure rather than input
                            # that should be repaired heuristically.
                            raise PatternRenderError(
                                f"tiling pattern /{selection.name} left graphics frames open"
                            )
                        self.resources = saved_resources  # type: ignore[attr-defined]
                        text.state = copy.deepcopy(saved_text_state)
                        text.in_text_object = saved_in_text
                        text.ctm = state.ctm
                        self.path = copy.deepcopy(saved_path)  # type: ignore[attr-defined]
                        self.pending_clip = saved_pending_clip  # type: ignore[attr-defined]
                        if hasattr(self, "_path_ctm"):
                            self._path_ctm = saved_path_ctm
                        if hasattr(self, "_mixed_path_ctm"):
                            self._mixed_path_ctm = saved_mixed_path_ctm
                        self._fill_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                        self._stroke_pattern_space = PatternColorSpaceState()  # type: ignore[attr-defined]
                        self._fill_pattern = None  # type: ignore[attr-defined]
                        self._stroke_pattern = None  # type: ignore[attr-defined]
        finally:
            self._tiling_pattern_depth -= 1
            surface.clip = parent_clip
            self.resources = saved_resources  # type: ignore[attr-defined]
            self.path = saved_path  # type: ignore[attr-defined]
            self.pending_clip = saved_pending_clip  # type: ignore[attr-defined]
            if hasattr(self, "_path_ctm"):
                self._path_ctm = saved_path_ctm
            if hasattr(self, "_mixed_path_ctm"):
                self._mixed_path_ctm = saved_mixed_path_ctm
            text.state = saved_text_state
            text.in_text_object = saved_in_text
            text.ctm = state.ctm
            self._fill_pattern_space = saved_fill_space  # type: ignore[attr-defined]
            self._stroke_pattern_space = saved_stroke_space  # type: ignore[attr-defined]
            self._fill_pattern = saved_fill_pattern  # type: ignore[attr-defined]
            self._stroke_pattern = saved_stroke_pattern  # type: ignore[attr-defined]
            self._pattern_stack[:] = saved_pattern_stack  # type: ignore[attr-defined]
