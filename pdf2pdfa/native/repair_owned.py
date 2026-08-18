"""Rendering-aware owned PDF/A repair pipeline.

PDF/A-1 transparency is routed to one of two owned static-visual repairs:

* page-content transparency -> rasterize page painting while preserving APs;
* annotation-appearance transparency -> rasterize the page with current normal
  annotation appearances and neutralize AP painting streams.

The annotation path supersedes the ordinary page path for the same page so a
page is never rasterized twice.
"""

from __future__ import annotations

from pathlib import Path
import re

from .annotation_flatten import AnnotationFlattenReport, flatten_pages_with_annotations
from .document import PDFDocument
from .flatten import FlattenReport, TransparencyFlattenError, flatten_pages
from .pdfa import NativePDFAValidator, policy
from .repair import (
    NativeRepairEngine,
    RepairPlan,
    UnsupportedNativeRepairError,
)


_PAGE_CONTENT_RE = re.compile(r"^page\[(\d+)\]/Contents(?:$|[#/])")
_ANNOTATION_RE = re.compile(r"^page\[(\d+)\]/Annots\[")


def flatten_page_numbers(plan: RepairPlan) -> tuple[int, ...]:
    value = getattr(plan, "flatten_pages", ())
    return tuple(int(item) for item in value)


def flatten_annotation_page_numbers(plan: RepairPlan) -> tuple[int, ...]:
    value = getattr(plan, "flatten_annotation_pages", ())
    return tuple(int(item) for item in value)


def has_visual_rewrite(plan: RepairPlan) -> bool:
    return bool(flatten_page_numbers(plan) or flatten_annotation_page_numbers(plan))


class OwnedRepairEngine(NativeRepairEngine):
    """Repair engine that consumes native rendering capabilities when needed."""

    def __init__(
        self,
        *,
        allow_attachment_removal: bool = False,
        transparency_dpi: int = 144,
    ) -> None:
        super().__init__(allow_attachment_removal=allow_attachment_removal)
        if transparency_dpi <= 0 or transparency_dpi > 2400:
            raise ValueError("transparency_dpi must be between 1 and 2400")
        self.transparency_dpi = transparency_dpi

    def plan(self, source: str | Path | bytes, level: str) -> RepairPlan:
        target = policy(level)
        plan = super().plan(source, target.level)
        setattr(plan, "flatten_pages", ())
        setattr(plan, "flatten_annotation_pages", ())

        if target.part != 1:
            return plan

        report = NativePDFAValidator().validate(source, target.level)
        failures = [
            failure for failure in report.failures if failure.rule_id == "pdfa1.transparency"
        ]
        if not failures:
            return plan

        # The base structural planner does not own a renderer and therefore
        # reports all PDF/A-1 transparency as one generic blocker. Rendering-
        # aware planning replaces only that blocker; all unrelated blockers stay.
        plan.blockers[:] = [
            blocker for blocker in plan.blockers if blocker.code != "pdfa1.transparency"
        ]

        content_pages: set[int] = set()
        annotation_pages: set[int] = set()
        unmapped: list[str] = []
        for failure in failures:
            annotation_match = _ANNOTATION_RE.match(failure.path)
            if annotation_match:
                annotation_pages.add(int(annotation_match.group(1)))
                continue
            content_match = _PAGE_CONTENT_RE.match(failure.path)
            if content_match:
                content_pages.add(int(content_match.group(1)))
                continue
            unmapped.append(failure.path)

        if unmapped:
            plan.block(
                "pdfa1.transparency_unmapped",
                "transparency was detected but could not be mapped to a page/annotation repair: "
                + ", ".join(unmapped),
            )
            return plan

        # Annotation-inclusive flatten already contains ordinary page painting,
        # so the same page shall never pass through both rasterization paths.
        content_pages.difference_update(annotation_pages)
        ordered_annotations = tuple(sorted(annotation_pages))
        ordered_content = tuple(sorted(content_pages))
        setattr(plan, "flatten_annotation_pages", ordered_annotations)
        setattr(plan, "flatten_pages", ordered_content)

        if ordered_annotations:
            plan.operations.insert(
                0,
                "flatten PDF/A-1 annotation appearance transparency on page(s) "
                + ", ".join(str(page) for page in ordered_annotations)
                + f" at {self.transparency_dpi} dpi",
            )
        if ordered_content:
            plan.operations.insert(
                0,
                "flatten PDF/A-1 page-content transparency on page(s) "
                + ", ".join(str(page) for page in ordered_content)
                + f" at {self.transparency_dpi} dpi",
            )
        return plan

    def repair_document(self, doc: PDFDocument, level: str, plan: RepairPlan) -> None:
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "native repair blocked: "
                + "; ".join(f"{item.code}: {item.message}" for item in plan.blockers)
            )

        annotation_pages = flatten_annotation_page_numbers(plan)
        if annotation_pages:
            try:
                annotation_report: AnnotationFlattenReport = flatten_pages_with_annotations(
                    doc,
                    annotation_pages,
                    dpi=self.transparency_dpi,
                )
            except TransparencyFlattenError as exc:
                raise UnsupportedNativeRepairError(str(exc)) from exc
            plan.operations.append(
                f"flattened {annotation_report.count} annotation-transparent page(s) with owned renderer; "
                f"neutralized {annotation_report.neutralized_appearances} appearance stream(s)"
            )

        pages = flatten_page_numbers(plan)
        if pages:
            try:
                report: FlattenReport = flatten_pages(
                    doc,
                    pages,
                    dpi=self.transparency_dpi,
                )
            except TransparencyFlattenError as exc:
                raise UnsupportedNativeRepairError(str(exc)) from exc
            plan.operations.append(
                f"flattened {report.count} transparent page(s) with owned renderer"
            )

        super().repair_document(doc, level, plan)
