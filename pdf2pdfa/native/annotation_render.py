"""Owned normal annotation-appearance compositor.

The renderer paints an annotation's current normal appearance (/AP /N) after
page content, using the PDF appearance mapping from the appearance Form BBox and
Matrix into the annotation Rect. Rollover/down appearances are interactive
states and are not part of the static page view used by visual fidelity.

Annotation painting runs in a fresh graphics/text state so the final state of a
page content stream cannot leak into annotation appearance interpretation.
"""

from __future__ import annotations

from decimal import Decimal

from .cff_text_render import OwnedOutlineTextRenderer
from .color import ColorSpace
from .objects import PDFDict, PDFName, PDFObject, PDFStream
from .page_render import GraphicsState, RenderingError, _initial_ctm
from .pattern_render import PatternColorSpaceState
from .raster import Color, Matrix, Path
from .structure import PageView, resolve


class AnnotationRenderingError(RenderingError):
    pass


def _number(doc, value: PDFObject, label: str) -> float:
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise AnnotationRenderingError(f"{label} shall be numeric")
    return float(value)


def _box(doc, value: PDFObject | None, label: str) -> tuple[float, float, float, float]:
    value = resolve(doc, value)
    if not isinstance(value, list) or len(value) != 4:
        raise AnnotationRenderingError(f"{label} shall contain four numbers")
    box = tuple(_number(doc, item, label) for item in value)
    if box[2] <= box[0] or box[3] <= box[1]:
        raise AnnotationRenderingError(f"{label} has non-positive dimensions")
    return box  # type: ignore[return-value]


def _matrix(doc, stream: PDFStream) -> Matrix:
    raw = resolve(doc, stream.get("Matrix")) if stream.get("Matrix") is not None else None
    if raw is None:
        return Matrix()
    if not isinstance(raw, list) or len(raw) != 6:
        raise AnnotationRenderingError("annotation appearance /Matrix shall contain six numbers")
    matrix = Matrix(*(_number(doc, item, "annotation appearance /Matrix") for item in raw))
    if abs(matrix.a * matrix.d - matrix.b * matrix.c) <= 1e-18:
        raise AnnotationRenderingError("annotation appearance /Matrix is singular")
    return matrix


def _transformed_bbox(matrix: Matrix, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    points = [
        matrix.transform(x0, y0), matrix.transform(x1, y0),
        matrix.transform(x1, y1), matrix.transform(x0, y1),
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _appearance_to_rect(
    doc,
    appearance: PDFStream,
    rect: tuple[float, float, float, float],
) -> Matrix:
    bbox = _box(doc, appearance.get("BBox"), "annotation appearance /BBox")
    form_matrix = _matrix(doc, appearance)
    tx0, ty0, tx1, ty1 = _transformed_bbox(form_matrix, bbox)
    tw = tx1 - tx0
    th = ty1 - ty0
    if tw <= 1e-18 or th <= 1e-18:
        raise AnnotationRenderingError("transformed annotation appearance BBox is degenerate")
    rw = rect[2] - rect[0]
    rh = rect[3] - rect[1]
    sx = rw / tw
    sy = rh / th
    return Matrix(sx, 0, 0, sy, rect[0] - tx0 * sx, rect[1] - ty0 * sy)


def _dict(doc, value: PDFObject | None) -> PDFDict | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFDict) else None


def _stream(doc, value: PDFObject | None) -> PDFStream | None:
    try:
        value = resolve(doc, value)
    except Exception:
        return None
    return value if isinstance(value, PDFStream) else None


def _normal_appearance(doc, annot: PDFDict) -> PDFStream | None:
    ap = _dict(doc, annot.get("AP"))
    if ap is None or ap.get("N") is None:
        return None
    normal = resolve(doc, ap.get("N"))
    if isinstance(normal, PDFStream):
        return normal
    if not isinstance(normal, PDFDict):
        raise AnnotationRenderingError("annotation /AP /N is neither stream nor state dictionary")

    states: dict[str, PDFStream] = {}
    for name, value in normal.items():
        stream = _stream(doc, value)
        if stream is None:
            raise AnnotationRenderingError(
                f"annotation /AP /N state /{name} is not an appearance stream"
            )
        states[name] = stream
    if not states:
        raise AnnotationRenderingError("annotation /AP /N state dictionary is empty")

    if annot.get("AS") is not None:
        state_value = resolve(doc, annot.get("AS"))
        if not isinstance(state_value, PDFName):
            raise AnnotationRenderingError("annotation /AS shall be a name")
        if state_value.value not in states:
            raise AnnotationRenderingError(
                f"annotation /AS /{state_value.value} does not select an /AP /N state"
            )
        return states[state_value.value]

    if len(states) == 1:
        return next(iter(states.values()))
    raise AnnotationRenderingError(
        "stateful annotation normal appearance requires a valid /AS selection"
    )


def _annotation_visible(doc, annot: PDFDict) -> bool:
    raw = resolve(doc, annot.get("F")) if annot.get("F") is not None else 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AnnotationRenderingError("annotation /F shall be an integer")
    if raw & (8 | 16 | 256):
        flags = []
        if raw & 8:
            flags.append("NoZoom")
        if raw & 16:
            flags.append("NoRotate")
        if raw & 256:
            flags.append("ToggleNoView")
        raise AnnotationRenderingError(
            "annotation display flag(s) require dedicated owned transform semantics: "
            + ", ".join(flags)
        )
    hidden = bool(raw & 2)
    invisible = bool(raw & 1)
    no_view = bool(raw & 32)
    return not (hidden or invisible or no_view)


class AnnotationAppearanceRendererMixin:
    """Paint current normal annotation appearances into the page raster."""

    def __init__(self, *args, render_annotations: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        self.render_annotations = bool(render_annotations)

    def render_page(self, page: PageView):
        rendered = super().render_page(page)  # type: ignore[misc]
        if self.render_annotations:
            page_ctm, _width, _height, _crop = _initial_ctm(page, self.dpi)
            self._paint_page_annotations(page, page_ctm)
        return rendered

    def _reset_annotation_extra_state(self) -> dict[str, object]:
        names = (
            "_fill_pattern_space", "_stroke_pattern_space",
            "_fill_pattern", "_stroke_pattern", "_pattern_stack",
            "_tiling_pattern_depth",
            "_fill_uncolored_space", "_stroke_uncolored_space",
            "_fill_uncolored_selection", "_stroke_uncolored_selection",
            "_uncolored_stack", "_uncolored_pattern_depth",
        )
        saved: dict[str, object] = {}
        for name in names:
            if not hasattr(self, name):
                continue
            value = getattr(self, name)
            saved[name] = list(value) if isinstance(value, list) else value
        if hasattr(self, "_fill_pattern_space"):
            self._fill_pattern_space = PatternColorSpaceState()
            self._stroke_pattern_space = PatternColorSpaceState()
            self._fill_pattern = None
            self._stroke_pattern = None
            self._pattern_stack = []
        if hasattr(self, "_tiling_pattern_depth"):
            self._tiling_pattern_depth = 0
        if hasattr(self, "_fill_uncolored_space"):
            self._fill_uncolored_space = None
            self._stroke_uncolored_space = None
            self._fill_uncolored_selection = None
            self._stroke_uncolored_selection = None
            self._uncolored_stack = []
            self._uncolored_pattern_depth = 0
        return saved

    def _restore_annotation_extra_state(self, saved: dict[str, object]) -> None:
        for name, value in saved.items():
            setattr(self, name, value)

    def _paint_annotation_appearance(
        self,
        appearance: PDFStream,
        *,
        rect: tuple[float, float, float, float],
        page_ctm: Matrix,
        page_resources: PDFDict,
    ) -> None:
        surface, _, _ = self._require()
        mapping = _appearance_to_rect(self.doc, appearance, rect)
        gray = ColorSpace("DeviceGray", 1, lambda values: (values[0], values[0], values[0]))
        fresh_state = GraphicsState(
            ctm=page_ctm.concat(mapping),
            fill_space=gray,
            stroke_space=gray,
            fill_color=Color(0, 0, 0, 1),
            stroke_color=Color(0, 0, 0, 1),
            clip=bytearray(surface.clip),
        )
        saved_state = self.state
        saved_text = self.text
        saved_resources = self.resources
        saved_path = self.path
        saved_pending = self.pending_clip
        saved_stack = self.stack
        saved_form_stack = set(self._form_stack)
        saved_soft = getattr(self, "soft_mask", None)
        saved_soft_stack = list(getattr(self, "_soft_stack", []))
        saved_path_ctm = getattr(self, "_path_ctm", None)
        saved_mixed = getattr(self, "_mixed_path_ctm", False)
        extras = self._reset_annotation_extra_state()
        try:
            self.state = fresh_state
            self.text = OwnedOutlineTextRenderer(
                surface,
                ctm=fresh_state.ctm,
                type3_painter=self._paint_type3_glyph,
            )
            self.resources = page_resources
            self.path = Path()
            self.pending_clip = None
            self.stack = []
            self._form_stack = set()
            if hasattr(self, "soft_mask"):
                self.soft_mask = None
                self._soft_stack = []
            if hasattr(self, "_path_ctm"):
                self._path_ctm = None
                self._mixed_path_ctm = False
            self._form(appearance)
            if self.stack:
                raise AnnotationRenderingError("annotation appearance leaked graphics-state frames")
            if self.text.in_text_object:
                raise AnnotationRenderingError("annotation appearance ended inside BT/ET")
        finally:
            self.state = saved_state
            self.text = saved_text
            self.resources = saved_resources
            self.path = saved_path
            self.pending_clip = saved_pending
            self.stack = saved_stack
            self._form_stack = saved_form_stack
            if hasattr(self, "soft_mask"):
                self.soft_mask = saved_soft
                self._soft_stack = saved_soft_stack
            if hasattr(self, "_path_ctm"):
                self._path_ctm = saved_path_ctm
                self._mixed_path_ctm = saved_mixed
            self._restore_annotation_extra_state(extras)

    def _paint_page_annotations(self, page: PageView, page_ctm: Matrix) -> None:
        raw_annots = resolve(self.doc, page.dictionary.get("Annots")) if page.dictionary.get("Annots") is not None else None
        if raw_annots is None:
            return
        if not isinstance(raw_annots, list):
            raise AnnotationRenderingError("page /Annots shall be an array")
        for index, raw in enumerate(raw_annots, start=1):
            annot = _dict(self.doc, raw)
            if annot is None:
                raise AnnotationRenderingError(f"annotation {index} is not a dictionary")
            if not _annotation_visible(self.doc, annot):
                continue
            appearance = _normal_appearance(self.doc, annot)
            if appearance is None:
                continue
            rect = _box(self.doc, annot.get("Rect"), f"annotation {index} /Rect")
            self._paint_annotation_appearance(
                appearance,
                rect=rect,
                page_ctm=page_ctm,
                page_resources=page.resources,
            )
