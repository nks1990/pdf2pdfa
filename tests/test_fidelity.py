"""Tests for the optional raster fidelity oracle."""

from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image

from pdf2pdfa.fidelity import VisualFidelityChecker


def _fake_renderer(change: bool = False, extra_page: bool = False):
    def render(self, pdf_path: Path, output_dir: Path, stem: str):
        first = output_dir / f"{stem}-0001.png"
        image = Image.new("RGB", (100, 100), "white")
        if change and stem == "candidate":
            for x in range(25):
                for y in range(25):
                    image.putpixel((x, y), (0, 0, 0))
        image.save(first)
        pages = [first]
        if extra_page and stem == "candidate":
            second = output_dir / f"{stem}-0002.png"
            Image.new("RGB", (100, 100), "white").save(second)
            pages.append(second)
        return pages

    return render


def _dummy_pdfs(tmp_path: Path):
    source = tmp_path / "source.pdf"
    candidate = tmp_path / "candidate.pdf"
    source.write_bytes(b"source")
    candidate.write_bytes(b"candidate")
    return source, candidate


def test_identical_render_passes(monkeypatch, tmp_path):
    checker = VisualFidelityChecker(executable="unused")
    monkeypatch.setattr(VisualFidelityChecker, "_render", _fake_renderer())
    source, candidate = _dummy_pdfs(tmp_path)

    report = checker.compare(source, candidate)

    assert report.available is True
    assert report.passed is True
    assert report.pages_compared == 1
    assert report.pages[0].changed_pixel_ratio == 0


def test_material_render_change_fails(monkeypatch, tmp_path):
    checker = VisualFidelityChecker(
        executable="unused",
        pixel_tolerance=4,
        max_mean_error=0.5,
        max_changed_pixel_ratio=0.01,
    )
    monkeypatch.setattr(
        VisualFidelityChecker,
        "_render",
        _fake_renderer(change=True),
    )
    source, candidate = _dummy_pdfs(tmp_path)

    report = checker.compare(source, candidate)

    assert report.passed is False
    assert report.pages[0].changed_pixel_ratio > 0.01


def test_page_count_change_fails(monkeypatch, tmp_path):
    checker = VisualFidelityChecker(executable="unused")
    monkeypatch.setattr(
        VisualFidelityChecker,
        "_render",
        _fake_renderer(extra_page=True),
    )
    source, candidate = _dummy_pdfs(tmp_path)

    report = checker.compare(source, candidate)

    assert report.passed is False
    assert report.source_pages == 1
    assert report.candidate_pages == 2
    assert report.reason == "Page count changed during conversion"
