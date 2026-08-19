"""Owned CIDFont vertical-writing metrics (DW2/W2).

For writing mode 1 each CID has a position vector ``v = (vx, vy)`` locating the
vertical origin relative to the glyph's horizontal origin, plus a displacement
``w = (0, wy)`` to the next text position. PDF's DW2 default is
``[880 -1000]``: default ``vy`` and ``wy``; default ``vx`` is half the glyph's
horizontal width. W2 overrides all three values for individual CIDs/ranges.

Range records stay compact: a malicious ``0 2147483647 ...`` record must never
allocate one dictionary entry per CID.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .document import PDFDocument
from .objects import PDFDict, PDFObject
from .structure import resolve


class VerticalMetricsError(ValueError):
    pass


_MAX_W2_ARRAY_METRICS = 1_000_000
_MAX_W2_RANGES = 100_000


@dataclass(frozen=True, slots=True)
class VerticalMetric:
    displacement_y: float
    position_x: float
    position_y: float


@dataclass(frozen=True, slots=True)
class VerticalMetricRange:
    start: int
    end: int
    metric: VerticalMetric

    def contains(self, cid: int) -> bool:
        return self.start <= cid <= self.end


def _number(doc: PDFDocument, value: PDFObject, label: str) -> float:
    value = resolve(doc, value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise VerticalMetricsError(f"{label} shall be numeric")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise VerticalMetricsError(f"{label} shall be finite")
    return number


def _exact_cid(doc: PDFDocument, value: PDFObject, label: str) -> int:
    number = _number(doc, value, label)
    cid = int(number)
    if cid != number or cid < 0:
        raise VerticalMetricsError(f"{label} shall be a non-negative integer CID")
    return cid


class VerticalMetrics:
    """Resolve writing-mode-1 metrics for a CIDFont dictionary."""

    def __init__(self, doc: PDFDocument, cidfont: PDFDict) -> None:
        self.doc = doc
        self.default_position_y = 880.0
        self.default_displacement_y = -1000.0
        self.overrides: dict[int, VerticalMetric] = {}
        self.ranges: list[VerticalMetricRange] = []
        self._parse_dw2(cidfont)
        self._parse_w2(cidfont)

    def _parse_dw2(self, cidfont: PDFDict) -> None:
        raw = resolve(self.doc, cidfont.get("DW2")) if cidfont.get("DW2") is not None else None
        if raw is None:
            return
        if not isinstance(raw, list) or len(raw) != 2:
            raise VerticalMetricsError("CIDFont /DW2 shall contain exactly two numbers")
        self.default_position_y = _number(self.doc, raw[0], "CIDFont /DW2[0]")
        self.default_displacement_y = _number(self.doc, raw[1], "CIDFont /DW2[1]")

    def _parse_w2(self, cidfont: PDFDict) -> None:
        raw = resolve(self.doc, cidfont.get("W2")) if cidfont.get("W2") is not None else None
        if raw is None:
            return
        if not isinstance(raw, list):
            raise VerticalMetricsError("CIDFont /W2 shall be an array")

        array_metrics = 0
        range_records = 0
        index = 0
        while index < len(raw):
            start = _exact_cid(self.doc, raw[index], "CIDFont /W2 start CID")
            index += 1
            if index >= len(raw):
                raise VerticalMetricsError("CIDFont /W2 ends after a start CID")
            second = resolve(self.doc, raw[index])
            index += 1

            if isinstance(second, list):
                if len(second) % 3:
                    raise VerticalMetricsError(
                        "CIDFont /W2 consecutive metric array length shall be a multiple of 3"
                    )
                count = len(second) // 3
                array_metrics += count
                if array_metrics > _MAX_W2_ARRAY_METRICS:
                    raise VerticalMetricsError("CIDFont /W2 contains too many explicit metrics")
                if start + count - 1 > 0x7FFFFFFF:
                    raise VerticalMetricsError("CIDFont /W2 explicit CID sequence exceeds owned limit")
                for offset in range(0, len(second), 3):
                    cid = start + offset // 3
                    self.overrides[cid] = VerticalMetric(
                        _number(self.doc, second[offset], f"CIDFont /W2 CID {cid} wy"),
                        _number(self.doc, second[offset + 1], f"CIDFont /W2 CID {cid} vx"),
                        _number(self.doc, second[offset + 2], f"CIDFont /W2 CID {cid} vy"),
                    )
                continue

            end = _exact_cid(self.doc, second, "CIDFont /W2 end CID")
            if end < start:
                raise VerticalMetricsError("CIDFont /W2 end CID precedes start CID")
            if index + 2 >= len(raw):
                raise VerticalMetricsError("CIDFont /W2 range is missing wy/vx/vy")
            metric = VerticalMetric(
                _number(self.doc, raw[index], "CIDFont /W2 range wy"),
                _number(self.doc, raw[index + 1], "CIDFont /W2 range vx"),
                _number(self.doc, raw[index + 2], "CIDFont /W2 range vy"),
            )
            index += 3
            range_records += 1
            if range_records > _MAX_W2_RANGES:
                raise VerticalMetricsError("CIDFont /W2 contains too many range records")
            self.ranges.append(VerticalMetricRange(start, end, metric))

    def metric(self, cid: int, horizontal_width: float) -> VerticalMetric:
        if cid < 0:
            raise VerticalMetricsError("CID cannot be negative")
        override = self.overrides.get(cid)
        if override is not None:
            return override
        # Later W2 records are more specific operationally if malformed input
        # overlaps ranges; looking in reverse gives deterministic last-record
        # precedence without expanding any range.
        for item in reversed(self.ranges):
            if item.contains(cid):
                return item.metric
        return VerticalMetric(
            self.default_displacement_y,
            float(horizontal_width) * 0.5,
            self.default_position_y,
        )
