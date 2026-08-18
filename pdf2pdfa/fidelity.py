"""Optional visual fidelity verification for converted PDFs.

The checker renders source and candidate through the same Ghostscript raster
pipeline, then compares the resulting page images. It is deliberately
orthogonal to PDF/A validation: veraPDF answers "is it conforming?", while
this module answers "did the rendered appearance materially change?".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal


FidelityMode = Literal["off", "warn", "strict"]


class FidelityError(RuntimeError):
    """Base class for visual-fidelity failures."""


class FidelityUnavailableError(FidelityError):
    """Raised when the optional visual-fidelity stack is unavailable."""


@dataclass(frozen=True, slots=True)
class PageFidelity:
    page: int
    width: int
    height: int
    mean_error: float
    changed_pixel_ratio: float
    max_error: int
    passed: bool


@dataclass(frozen=True, slots=True)
class FidelityReport:
    passed: bool
    available: bool
    source_pages: int
    candidate_pages: int
    pages_compared: int
    dpi: int
    pixel_tolerance: int
    max_mean_error: float
    max_changed_pixel_ratio: float
    pages: tuple[PageFidelity, ...] = ()
    reason: str | None = None

    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        dpi: int,
        pixel_tolerance: int,
        max_mean_error: float,
        max_changed_pixel_ratio: float,
    ) -> "FidelityReport":
        return cls(
            passed=False,
            available=False,
            source_pages=0,
            candidate_pages=0,
            pages_compared=0,
            dpi=dpi,
            pixel_tolerance=pixel_tolerance,
            max_mean_error=max_mean_error,
            max_changed_pixel_ratio=max_changed_pixel_ratio,
            pages=(),
            reason=reason,
        )


def _discover_ghostscript() -> str | None:
    for candidate in ("gs", "gswin64c", "gswin32c"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


class VisualFidelityChecker:
    """Compare source/candidate page renders with bounded raster tolerances."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout: int = 300,
        dpi: int = 120,
        pixel_tolerance: int = 12,
        max_mean_error: float = 2.0,
        max_changed_pixel_ratio: float = 0.02,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if dpi <= 0:
            raise ValueError("dpi must be positive")
        if not 0 <= pixel_tolerance <= 255:
            raise ValueError("pixel_tolerance must be between 0 and 255")
        if max_mean_error < 0:
            raise ValueError("max_mean_error must be non-negative")
        if not 0 <= max_changed_pixel_ratio <= 1:
            raise ValueError("max_changed_pixel_ratio must be between 0 and 1")
        self.executable = executable or _discover_ghostscript()
        self.timeout = timeout
        self.dpi = dpi
        self.pixel_tolerance = pixel_tolerance
        self.max_mean_error = max_mean_error
        self.max_changed_pixel_ratio = max_changed_pixel_ratio

    def available(self) -> bool:
        if not self.executable:
            return False
        return bool(shutil.which(self.executable) or Path(self.executable).is_file())

    @staticmethod
    def _pillow():
        try:
            from PIL import Image, ImageChops, ImageStat
        except ImportError as exc:
            raise FidelityUnavailableError(
                "Visual fidelity checking requires Pillow; install pdf2pdfa[fidelity]"
            ) from exc
        return Image, ImageChops, ImageStat

    def _render(self, pdf_path: Path, output_dir: Path, stem: str) -> list[Path]:
        if not self.available():
            raise FidelityUnavailableError(
                "Visual fidelity checking requires Ghostscript (gs/gswin*c)"
            )

        pattern = output_dir / f"{stem}-%04d.png"
        cmd = [
            str(self.executable),
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dQUIET",
            "-sDEVICE=png16m",
            f"-r{self.dpi}",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            f"-sOutputFile={pattern}",
            str(pdf_path),
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FidelityError(
                f"Ghostscript fidelity render timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise FidelityUnavailableError(
                f"Could not execute Ghostscript for fidelity checking: {exc}"
            ) from exc

        pages = sorted(output_dir.glob(f"{stem}-*.png"))
        if completed.returncode != 0 or not pages:
            diagnostic = completed.stderr.strip() or completed.stdout.strip()
            raise FidelityError(
                f"Ghostscript fidelity render failed (exit {completed.returncode}): "
                f"{diagnostic[-4000:]}"
            )
        return pages

    def compare(
        self,
        source_path: str | Path,
        candidate_path: str | Path,
    ) -> FidelityReport:
        """Render and compare *source_path* and *candidate_path* page-by-page."""

        Image, ImageChops, ImageStat = self._pillow()
        source_path = Path(source_path).resolve()
        candidate_path = Path(candidate_path).resolve()

        with tempfile.TemporaryDirectory(prefix="pdf2pdfa-fidelity-") as tempdir_name:
            tempdir = Path(tempdir_name)
            source_pages = self._render(source_path, tempdir, "source")
            candidate_pages = self._render(candidate_path, tempdir, "candidate")

            if len(source_pages) != len(candidate_pages):
                return FidelityReport(
                    passed=False,
                    available=True,
                    source_pages=len(source_pages),
                    candidate_pages=len(candidate_pages),
                    pages_compared=0,
                    dpi=self.dpi,
                    pixel_tolerance=self.pixel_tolerance,
                    max_mean_error=self.max_mean_error,
                    max_changed_pixel_ratio=self.max_changed_pixel_ratio,
                    reason="Page count changed during conversion",
                )

            page_reports: list[PageFidelity] = []
            for index, (src_path, dst_path) in enumerate(
                zip(source_pages, candidate_pages), start=1
            ):
                with Image.open(src_path) as src_img, Image.open(dst_path) as dst_img:
                    src = src_img.convert("RGB")
                    dst = dst_img.convert("RGB")
                    if src.size != dst.size:
                        page_reports.append(
                            PageFidelity(
                                page=index,
                                width=dst.width,
                                height=dst.height,
                                mean_error=255.0,
                                changed_pixel_ratio=1.0,
                                max_error=255,
                                passed=False,
                            )
                        )
                        continue

                    diff = ImageChops.difference(src, dst)
                    stat = ImageStat.Stat(diff)
                    mean_error = float(sum(stat.mean) / len(stat.mean))
                    gray = diff.convert("L")
                    histogram = gray.histogram()
                    changed = sum(histogram[self.pixel_tolerance + 1 :])
                    pixels = max(1, src.width * src.height)
                    changed_ratio = changed / pixels
                    max_error = max(
                        (value for value, count in enumerate(histogram) if count),
                        default=0,
                    )
                    passed = (
                        mean_error <= self.max_mean_error
                        and changed_ratio <= self.max_changed_pixel_ratio
                    )
                    page_reports.append(
                        PageFidelity(
                            page=index,
                            width=src.width,
                            height=src.height,
                            mean_error=mean_error,
                            changed_pixel_ratio=changed_ratio,
                            max_error=max_error,
                            passed=passed,
                        )
                    )

            all_passed = all(page.passed for page in page_reports)
            return FidelityReport(
                passed=all_passed,
                available=True,
                source_pages=len(source_pages),
                candidate_pages=len(candidate_pages),
                pages_compared=len(page_reports),
                dpi=self.dpi,
                pixel_tolerance=self.pixel_tolerance,
                max_mean_error=self.max_mean_error,
                max_changed_pixel_ratio=self.max_changed_pixel_ratio,
                pages=tuple(page_reports),
                reason=None if all_passed else "Rendered appearance changed beyond tolerance",
            )
