"""Owned PDF pattern-color state and PatternType 2 shading fills.

This mixin implements the common shading-pattern path without duplicating the
shading evaluator. It owns ``/Pattern cs``, ``scn`` selection, q/Q preservation,
Pattern Matrix, a bounded Pattern ExtGState subset and path-clipped painting.

Current scope is deliberately explicit:

* PatternType 2 is supported for nonstroking path fills;
* PatternType 1 tiling patterns are a separate renderer workstream;
* pattern-colored strokes remain fail-closed;
* uncolored tiling pattern color spaces (``[/Pattern base]``) are recognized
  but cannot be used with a shading pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .content import ContentInstruction
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import (
    RenderingError,
    UnsupportedRenderingError,
    _name,
    _number,
    _resolve_resource,
)
from .raster import Matrix, rasterize_fill
from .shading import ShadingError, UnsupportedShadingError, paint_shading
from .structure import resolve


class PatternRenderError(ValueError):
    pass


class UnsupportedPatternError(PatternRenderError):
    pass


@dataclass(frozen=True, slots=True)
class PatternColorSpaceState:
    active: bool = False
    has_base_space: bool = False


@dataclass(frozen=True, slots=True)
class PatternSelection:
    name: str
    value: PDFObject
    pattern_type: int


def _exact_integer(value: PDFObject | None, label: str) -> int:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PatternRenderError(f"{label} shall be an integer")
    integer = int(value)
    if integer != value:
        raise PatternRenderError(f"{label} shall be an integer")
    return integer


def _pattern_dictionary(doc, value: PDFObject, label: str) -> PDFDict:
    resolved = resolve(doc, value)
    if isinstance(resolved, PDFStream):
        return resolved.dictionary
    if not isinstance(resolved, PDFDict):
        raise PatternRenderError(f"{label} is not a pattern dictionary/stream")
    return resolved


def _pattern_matrix(doc, dictionary: PDFDict) -> Matrix:
    raw = dictionary.get("Matrix")
    if raw is None:
        return Matrix()
    raw = resolve(doc, raw)
    if not isinstance(raw, list) or len(raw) != 6:
        raise PatternRenderError("Pattern /Matrix shall contain six numbers")
    values = [_number(resolve(doc, item), "Pattern/Matrix") for item in raw]
    matrix = Matrix(*values)
    determinant = matrix.a * matrix.d - matrix.b * matrix.c
    if abs(determinant) < 1e-15:
        raise PatternRenderError("Pattern /Matrix is singular")
    return matrix


def _pattern_extgstate(doc, dictionary: PDFDict, *, alpha: float, blend_mode: str) -> tuple[float, str]:
    raw = dictionary.get("ExtGState")
    if raw is None:
        return alpha, blend_mode
    raw = resolve(doc, raw)
    if not isinstance(raw, PDFDict):
        raise PatternRenderError("shading pattern /ExtGState is not a dictionary")

    allowed = {"Type", "ca", "CA", "BM", "SMask"}
    unexpected = sorted(key for key in raw if key not in allowed)
    if unexpected:
        raise UnsupportedPatternError(
            "shading pattern ExtGState contains unsupported parameter(s): "
            + ", ".join("/" + key for key in unexpected)
        )

    if raw.get("ca") is not None:
        alpha = _number(resolve(doc, raw.get("ca")), "Pattern/ExtGState/ca")
        if not 0.0 <= alpha <= 1.0:
            raise PatternRenderError("Pattern ExtGState /ca shall be between 0 and 1")

    if raw.get("BM") is not None:
        bm = resolve(doc, raw.get("BM"))
        if isinstance(bm, list):
            if not bm:
                raise PatternRenderError("Pattern ExtGState /BM array is empty")
            bm = resolve(doc, bm[0])
        if not isinstance(bm, PDFName):
            raise PatternRenderError("Pattern ExtGState /BM is not a name")
        blend_mode = bm.value

    if raw.get("SMask") is not None:
        smask = resolve(doc, raw.get("SMask"))
        if not isinstance(smask, PDFName) or smask.value != "None":
            raise UnsupportedPatternError(
                "shading pattern ExtGState /SMask requires nested owned soft-mask composition"
            )
    return alpha, blend_mode


class PatternShadingRendererMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        self._fill_pattern_space = PatternColorSpaceState()
        self._stroke_pattern_space = PatternColorSpaceState()
        self._fill_pattern: PatternSelection | None = None
        self._stroke_pattern: PatternSelection | None = None
        self._pattern_stack: list[
            tuple[
                PatternColorSpaceState,
                PatternColorSpaceState,
                PatternSelection | None,
                PatternSelection | None,
            ]
        ] = []

    def _pattern_colorspace(self, operand: PDFObject, *, stroking: bool) -> bool:
        name = _name(operand, "CS/cs")
        value: PDFObject
        if name == "Pattern":
            value = PDFName("Pattern")
        else:
            try:
                value = _resolve_resource(self.doc, self.resources, "ColorSpace", name)  # type: ignore[attr-defined]
            except RenderingError:
                return False
        value = resolve(self.doc, value)  # type: ignore[attr-defined]

        state: PatternColorSpaceState | None = None
        if isinstance(value, PDFName) and value.value == "Pattern":
            state = PatternColorSpaceState(True, False)
        elif isinstance(value, list) and value:
            family = resolve(self.doc, value[0])  # type: ignore[attr-defined]
            if isinstance(family, PDFName) and family.value == "Pattern":
                if len(value) != 2:
                    raise PatternRenderError(
                        "uncolored Pattern color space shall contain exactly one base color space"
                    )
                state = PatternColorSpaceState(True, True)
        if state is None:
            return False

        if stroking:
            self._stroke_pattern_space = state
            self._stroke_pattern = None
        else:
            self._fill_pattern_space = state
            self._fill_pattern = None
        return True

    def _select_pattern(self, args: list[PDFObject], *, stroking: bool) -> None:
        space = self._stroke_pattern_space if stroking else self._fill_pattern_space
        if not space.active:
            raise PatternRenderError("pattern selection used outside a Pattern color space")
        if not args:
            raise PatternRenderError("SCN/scn in Pattern color space requires a pattern name")
        raw_name = resolve(self.doc, args[-1])  # type: ignore[attr-defined]
        if not isinstance(raw_name, PDFName):
            raise PatternRenderError("last SCN/scn operand shall be a pattern name")
        color_components = args[:-1]
        if space.has_base_space:
            # Base-space components are meaningful only for uncolored tiling
            # patterns (PaintType 2). Type2 shading patterns are intrinsically
            # colored and therefore cannot consume them.
            if not color_components:
                raise PatternRenderError(
                    "uncolored Pattern color space requires base color components"
                )
        elif color_components:
            raise PatternRenderError(
                "colored Pattern color space shall not include base color components"
            )

        value = _resolve_resource(
            self.doc, self.resources, "Pattern", raw_name.value  # type: ignore[attr-defined]
        )
        dictionary = _pattern_dictionary(self.doc, value, f"Pattern /{raw_name.value}")  # type: ignore[attr-defined]
        pattern_type = _exact_integer(
            resolve(self.doc, dictionary.get("PatternType")),  # type: ignore[attr-defined]
            "PatternType",
        )
        selection = PatternSelection(raw_name.value, value, pattern_type)
        if stroking:
            self._stroke_pattern = selection
        else:
            self._fill_pattern = selection

    def _instruction(self, instruction: ContentInstruction) -> None:
        op = instruction.operator
        args = list(instruction.operands)

        if op == "q":
            self._pattern_stack.append(
                (
                    self._fill_pattern_space,
                    self._stroke_pattern_space,
                    self._fill_pattern,
                    self._stroke_pattern,
                )
            )
            return super()._instruction(instruction)  # type: ignore[misc]

        if op == "Q":
            result = super()._instruction(instruction)  # type: ignore[misc]
            if not self._pattern_stack:
                raise RenderingError("pattern graphics-state stack underflow")
            (
                self._fill_pattern_space,
                self._stroke_pattern_space,
                self._fill_pattern,
                self._stroke_pattern,
            ) = self._pattern_stack.pop()
            return result

        if op in {"cs", "CS"}:
            if len(args) != 1:
                raise RenderingError(f"{op} expects one color-space name")
            stroking = op == "CS"
            if self._pattern_colorspace(args[0], stroking=stroking):
                return
            if stroking:
                self._stroke_pattern_space = PatternColorSpaceState()
                self._stroke_pattern = None
            else:
                self._fill_pattern_space = PatternColorSpaceState()
                self._fill_pattern = None
            return super()._instruction(instruction)  # type: ignore[misc]

        if op in {"scn", "SCN"}:
            stroking = op == "SCN"
            space = self._stroke_pattern_space if stroking else self._fill_pattern_space
            if space.active:
                self._select_pattern(args, stroking=stroking)
                return

        if op in {"sc", "SC"}:
            stroking = op == "SC"
            space = self._stroke_pattern_space if stroking else self._fill_pattern_space
            if space.active:
                raise UnsupportedPatternError(
                    f"{op} cannot select a pattern; use {'SCN' if stroking else 'scn'} with a pattern name"
                )

        # Device color operators replace Pattern color state on the respective
        # side exactly as selecting a non-Pattern cs/CS would.
        if op in {"g", "rg", "k"}:
            self._fill_pattern_space = PatternColorSpaceState()
            self._fill_pattern = None
        elif op in {"G", "RG", "K"}:
            self._stroke_pattern_space = PatternColorSpaceState()
            self._stroke_pattern = None

        return super()._instruction(instruction)  # type: ignore[misc]

    def _paint_pattern_fill(self, *, even_odd: bool) -> None:
        selection = self._fill_pattern
        if selection is None:
            raise PatternRenderError(
                "Pattern fill color space has no selected pattern (missing scn)"
            )
        if selection.pattern_type != 2:
            raise UnsupportedPatternError(
                f"owned renderer currently supports PatternType 2 shading fills; "
                f"/{selection.name} is PatternType {selection.pattern_type}"
            )
        if self._fill_pattern_space.has_base_space:
            raise UnsupportedPatternError(
                "PatternType 2 cannot be painted through an uncolored Pattern base color space"
            )

        dictionary = _pattern_dictionary(
            self.doc, selection.value, f"Pattern /{selection.name}"  # type: ignore[attr-defined]
        )
        shading = dictionary.get("Shading")
        if shading is None:
            raise PatternRenderError(f"shading pattern /{selection.name} has no /Shading")
        surface, state, _ = self._require()  # type: ignore[attr-defined]
        mask = rasterize_fill(
            self.path,  # type: ignore[attr-defined]
            surface.width,
            surface.height,
            even_odd=even_odd,
        )
        old_clip = bytearray(surface.clip)
        surface.clip = bytearray(
            (old_clip[index] * mask[index] + 127) // 255
            for index in range(len(old_clip))
        )
        try:
            matrix = _pattern_matrix(self.doc, dictionary)  # type: ignore[attr-defined]
            alpha, blend_mode = _pattern_extgstate(
                self.doc,  # type: ignore[attr-defined]
                dictionary,
                alpha=state.fill_alpha,
                blend_mode=state.blend_mode,
            )
            try:
                paint_shading(
                    self.doc,  # type: ignore[attr-defined]
                    shading,
                    resources=self.resources,  # type: ignore[attr-defined]
                    surface=surface,
                    ctm=state.ctm.concat(matrix),
                    fill_alpha=alpha,
                    blend_mode=blend_mode,
                    soft_mask=getattr(self, "soft_mask", None),
                )
            except UnsupportedShadingError as exc:
                raise UnsupportedRenderingError(str(exc)) from exc
            except ShadingError as exc:
                raise RenderingError(str(exc)) from exc
        finally:
            surface.clip = old_clip

    def _paint_operator(self, op: str, args: list[PDFObject]) -> None:
        fill = op in {"f", "F", "f*", "B", "B*", "b", "b*"}
        stroke = op in {"S", "s", "B", "B*", "b", "b*"}

        if stroke and self._stroke_pattern_space.active:
            raise UnsupportedPatternError(
                "pattern-colored strokes require an owned pattern-stroke geometry path"
            )
        if not (fill and self._fill_pattern_space.active):
            return super()._paint_operator(op, args)  # type: ignore[misc]
        if args:
            raise RenderingError(f"{op} takes no operands")

        close = op in {"b", "b*"}
        if close and self.path.subpaths:  # type: ignore[attr-defined]
            self.path.close()  # type: ignore[attr-defined]
        even_odd = op in {"f*", "B*", "b*"}
        self._paint_pattern_fill(even_odd=even_odd)

        # A combined fill+stroke operator must still paint the ordinary solid
        # stroke over the same path. Delegate only the stroke operation so the
        # base renderer does not repaint a solid fill over the pattern.
        if stroke:
            return super()._paint_operator("S", [])  # type: ignore[misc]

        self._apply_pending_clip()  # type: ignore[attr-defined]
        self.path.clear()  # type: ignore[attr-defined]
        self.pending_clip = None  # type: ignore[attr-defined]
        if hasattr(self, "_path_ctm"):
            self._path_ctm = None
        if hasattr(self, "_mixed_path_ctm"):
            self._mixed_path_ctm = False
