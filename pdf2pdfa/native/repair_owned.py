"""Full owned repair pipeline layered on the conservative structural repair.

This module promotes PDF/A-1 page-content transparency from a hard blocker to
an owned repair. Annotation appearance transparency remains fail-closed until
annotation appearance composition is implemented by the renderer.
"""

from __future__ import annotations

from pathlib import Path
import re

from .document import PDFDocument
from .flatten import FlattenReport, TransparencyFlattenError, flatten_pages
from .pdfa import NativePDFAValidator, policy
from .repair import (
    NativeRepairEngine,
    RepairPlan,
    UnsupportedNativeRepairError,
)


_PAGE_CONTENT_RE = re.compile(r"^page\[(\d+)\]/Contents(?:$|[#/])")


def flatten_page_numbers(plan: RepairPlan) -> tuple[int, ...]:
    value = getattr(plan, "flatten_pages", ())
    return tuple(int(item) for item in value)


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

        if target.part != 1:
            return plan

        report = NativePDFAValidator().validate(source, target.level)
        failures = [
            failure for failure in report.failures if failure.rule_id == "pdfa1.transparency"
        ]
        if not failures:
            return plan

        # The base structural planner does not own a renderer and therefore
        # reports all PDF/A-1 transparency as one generic blocker. Remove only
        # that blocker here; every other blocker remains authoritative.
        plan.blockers[:] = [
            blocker for blocker in plan.blockers if blocker.code != "pdfa1.transparency"
        ]

        annotation_failures = [
            failure for failure in failures if "/Annots[" in failure.path or "/AP/" in failure.path
        ]
        if annotation_failures:
            for failure in annotation_failures:
                plan.block(
                    "pdfa1.annotation_transparency",
                    "annotation appearance transparency requires owned annotation composition before flattening",
                    failure.path,
                )
            return plan

        pages: set[int] = set()
        for failure in failures:
            match = _PAGE_CONTENT_RE.match(failure.path)
            if match:
                pages.add(int(match.group(1)))

        if not pages:
            # A reachable transparent object that cannot be associated with a
            # page painting stream must not be guessed away. This catches future
            # transparency mechanisms until the validator/planner maps them.
            plan.block(
                "pdfa1.transparency_unmapped",
                "transparency was detected but could not be mapped to a page content stream safely",
            )
            return plan

        ordered = tuple(sorted(pages))
        setattr(plan, "flatten_pages", ordered)
        plan.operations.insert(
            0,
            "flatten PDF/A-1 transparency on page(s) "
            + ", ".join(str(page) for page in ordered)
            + f" at {self.transparency_dpi} dpi",
        )
        return plan

    def repair_document(self, doc: PDFDocument, level: str, plan: RepairPlan) -> None:
        if plan.blockers:
            raise UnsupportedNativeRepairError(
                "native repair blocked: "
                + "; ".join(f"{item.code}: {item.message}" for item in plan.blockers)
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
