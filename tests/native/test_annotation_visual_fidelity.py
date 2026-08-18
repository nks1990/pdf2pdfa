from __future__ import annotations

from pdf2pdfa.native.visual_fidelity import NativeVisualFidelityChecker

from tests.native.test_annotation_render import _appearance, _pdf


def test_visual_fidelity_detects_annotation_appearance_drift():
    red = _pdf(_appearance(b"1 0 0 rg 0 0 10 10 re f\n"))
    green = _pdf(_appearance(b"0 1 0 rg 0 0 10 10 re f\n"))
    report = NativeVisualFidelityChecker(
        dpi=72,
        pixel_tolerance=0,
        max_mean_error=0,
        max_changed_pixel_ratio=0,
    ).compare(red, green)
    assert not report.passed
    assert report.pages[0].changed_pixel_ratio > 0
    assert report.pages[0].max_channel_error > 200


def test_visual_fidelity_accepts_identical_annotation_appearance():
    source = _pdf(_appearance(b"1 0 0 rg 0 0 10 10 re f\n"))
    report = NativeVisualFidelityChecker(
        dpi=72,
        pixel_tolerance=0,
        max_mean_error=0,
        max_changed_pixel_ratio=0,
    ).compare(source, source)
    assert report.passed, report.differences
