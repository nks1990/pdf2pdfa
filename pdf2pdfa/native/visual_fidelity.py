"""Owned visual-fidelity gate backed by the pure-Python page renderer.

No image library or external rasterizer is involved. Source and candidate are
rendered through the same owned transparency-capable interpreter and compared
in memory. The checker is deliberately fail-closed: an unsupported painting
feature makes fidelity unavailable rather than being skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .document import PDFDocument
from .page_render import RenderingError, UnsupportedRenderingError
from .structure import PageView, walk_pages
from .transparency_render import TransparencyRenderer


class VisualFidelityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VisualPageReport:
    page_number: int
    width: int
    height: int
    mean_channel_error: float
    changed_pixel_ratio: float
    max_channel_error: int
    passed: bool


@dataclass(frozen=True, slots=True)
class VisualFidelityReport:
    passed: bool
    dpi: int
    pixel_tolerance: int
    max_mean_error: float
    max_changed_pixel_ratio: float
    pages: tuple[VisualPageReport, ...]
    differences: tuple[str, ...] = ()
    engine: str = "pdf2pdfa-owned-visual"


def _document(source: str | Path | bytes | PDFDocument) -> PDFDocument:
    return source if isinstance(source, PDFDocument) else PDFDocument.open(source, repair=False)


def _same_box(left, right) -> bool:
    return tuple(left) == tuple(right)


class NativeVisualFidelityChecker:
    """Render source/candidate with owned code and compare RGB appearance."""

    def __init__(
        self,
        *,
        dpi: int = 144,
        pixel_tolerance: int = 2,
        max_mean_error: float = 1.0,
        max_changed_pixel_ratio: float = 0.01,
    ) -> None:
        if dpi <= 0 or dpi > 2400:
            raise ValueError("dpi must be between 1 and 2400")
        if not 0 <= pixel_tolerance <= 255:
            raise ValueError("pixel_tolerance must be between 0 and 255")
        if max_mean_error < 0:
            raise ValueError("max_mean_error must be non-negative")
        if not 0 <= max_changed_pixel_ratio <= 1:
            raise ValueError("max_changed_pixel_ratio must be between 0 and 1")
        self.dpi = dpi
        self.pixel_tolerance = pixel_tolerance
        self.max_mean_error = max_mean_error
        self.max_changed_pixel_ratio = max_changed_pixel_ratio

    def _render(self, doc: PDFDocument, page: PageView, page_number: int):
        try:
            return TransparencyRenderer(doc, dpi=self.dpi).render_page(page)
        except (UnsupportedRenderingError, RenderingError, ValueError) as exc:
            raise VisualFidelityError(
                f"owned renderer cannot evaluate page {page_number}: {exc}"
            ) from exc

    def _compare_pixels(
        self,
        page_number: int,
        width: int,
        height: int,
        left: bytes,
        right: bytes,
    ) -> VisualPageReport:
        expected = width * height * 3
        if len(left) != expected or len(right) != expected:
            raise VisualFidelityError(
                f"page {page_number} renderer returned inconsistent RGB byte counts"
            )

        changed = 0
        total_error = 0
        max_error = 0
        pixels = max(1, width * height)
        for offset in range(0, expected, 3):
            dr = abs(left[offset] - right[offset])
            dg = abs(left[offset + 1] - right[offset + 1])
            db = abs(left[offset + 2] - right[offset + 2])
            local_max = max(dr, dg, db)
            if local_max > self.pixel_tolerance:
                changed += 1
            max_error = max(max_error, local_max)
            total_error += dr + dg + db

        mean_error = total_error / (pixels * 3)
        changed_ratio = changed / pixels
        passed = (
            mean_error <= self.max_mean_error
            and changed_ratio <= self.max_changed_pixel_ratio
        )
        return VisualPageReport(
            page_number=page_number,
            width=width,
            height=height,
            mean_channel_error=mean_error,
            changed_pixel_ratio=changed_ratio,
            max_channel_error=max_error,
            passed=passed,
        )

    def compare(
        self,
        source: str | Path | bytes | PDFDocument,
        candidate: str | Path | bytes | PDFDocument,
    ) -> VisualFidelityReport:
        before = _document(source)
        after = _document(candidate)
        left_pages = list(walk_pages(before))
        right_pages = list(walk_pages(after))
        differences: list[str] = []

        if len(left_pages) != len(right_pages):
            differences.append(
                f"page count changed from {len(left_pages)} to {len(right_pages)}"
            )

        reports: list[VisualPageReport] = []
        for page_number, (left_page, right_page) in enumerate(
            zip(left_pages, right_pages), start=1
        ):
            if not _same_box(left_page.media_box, right_page.media_box):
                differences.append(f"page {page_number} MediaBox changed")
            if not _same_box(left_page.crop_box, right_page.crop_box):
                differences.append(f"page {page_number} CropBox changed")
            if left_page.rotate != right_page.rotate:
                differences.append(f"page {page_number} Rotate changed")

            left_render = self._render(before, left_page, page_number)
            right_render = self._render(after, right_page, page_number)
            if (
                left_render.width != right_render.width
                or left_render.height != right_render.height
            ):
                differences.append(
                    f"page {page_number} raster dimensions changed from "
                    f"{left_render.width}x{left_render.height} to "
                    f"{right_render.width}x{right_render.height}"
                )
                reports.append(
                    VisualPageReport(
                        page_number,
                        right_render.width,
                        right_render.height,
                        255.0,
                        1.0,
                        255,
                        False,
                    )
                )
                continue

            report = self._compare_pixels(
                page_number,
                left_render.width,
                left_render.height,
                left_render.rgb_bytes(),
                right_render.rgb_bytes(),
            )
            reports.append(report)
            if not report.passed:
                differences.append(
                    f"page {page_number} visual drift: mean={report.mean_channel_error:.4f}, "
                    f"changed={report.changed_pixel_ratio:.6f}, max={report.max_channel_error}"
                )

        return VisualFidelityReport(
            passed=not differences and all(page.passed for page in reports),
            dpi=self.dpi,
            pixel_tolerance=self.pixel_tolerance,
            max_mean_error=self.max_mean_error,
            max_changed_pixel_ratio=self.max_changed_pixel_ratio,
            pages=tuple(reports),
            differences=tuple(differences),
        )
