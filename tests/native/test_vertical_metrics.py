from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName
from pdf2pdfa.native.vertical_metrics import VerticalMetric, VerticalMetrics, VerticalMetricsError


def _doc() -> PDFDocument:
    builder = PDFBuilder(version="1.7")
    pages_ref = builder.add(PDFDict({"Type": PDFName("Pages"), "Count": 0, "Kids": []}))
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False)


def test_default_dw2_uses_half_horizontal_width_for_vx():
    metrics = VerticalMetrics(_doc(), PDFDict())
    assert metrics.metric(10, 600) == VerticalMetric(-1000.0, 300.0, 880.0)


def test_custom_dw2_changes_default_vy_and_wy_only():
    metrics = VerticalMetrics(_doc(), PDFDict({"DW2": [900, -1200]}))
    assert metrics.metric(7, 500) == VerticalMetric(-1200.0, 250.0, 900.0)


def test_w2_consecutive_array_assigns_triplets_per_cid():
    metrics = VerticalMetrics(
        _doc(),
        PDFDict({"W2": [10, [-900, 300, 700, -1000, 400, 800]]}),
    )
    assert metrics.metric(10, 999) == VerticalMetric(-900.0, 300.0, 700.0)
    assert metrics.metric(11, 999) == VerticalMetric(-1000.0, 400.0, 800.0)
    assert metrics.metric(12, 600) == VerticalMetric(-1000.0, 300.0, 880.0)


def test_w2_range_stays_compact_even_for_huge_cid_span():
    metrics = VerticalMetrics(
        _doc(),
        PDFDict({"W2": [0, 2_000_000_000, -1100, 250, 750]}),
    )
    assert metrics.overrides == {}
    assert len(metrics.ranges) == 1
    assert metrics.metric(0, 500) == VerticalMetric(-1100.0, 250.0, 750.0)
    assert metrics.metric(1_500_000_000, 500) == VerticalMetric(-1100.0, 250.0, 750.0)
    assert metrics.metric(2_000_000_000, 500) == VerticalMetric(-1100.0, 250.0, 750.0)


def test_later_w2_range_has_deterministic_precedence_when_ranges_overlap():
    metrics = VerticalMetrics(
        _doc(),
        PDFDict(
            {
                "W2": [
                    10, 20, -900, 100, 700,
                    15, 25, -1200, 300, 800,
                ]
            }
        ),
    )
    assert metrics.metric(12, 500) == VerticalMetric(-900.0, 100.0, 700.0)
    assert metrics.metric(17, 500) == VerticalMetric(-1200.0, 300.0, 800.0)


@pytest.mark.parametrize(
    "dictionary, message",
    [
        (PDFDict({"DW2": [880]}), "exactly two"),
        (PDFDict({"DW2": [True, -1000]}), "numeric"),
        (PDFDict({"W2": [10]}), "ends after"),
        (PDFDict({"W2": [10, [-900, 300]]}), "multiple of 3"),
        (PDFDict({"W2": [20, 10, -900, 300, 700]}), "precedes"),
        (PDFDict({"W2": [-1, [-900, 300, 700]]}), "non-negative"),
        (PDFDict({"W2": [10.5, [-900, 300, 700]]}), "integer CID"),
    ],
)
def test_invalid_vertical_metrics_fail_closed(dictionary: PDFDict, message: str):
    with pytest.raises(VerticalMetricsError, match=message):
        VerticalMetrics(_doc(), dictionary)
