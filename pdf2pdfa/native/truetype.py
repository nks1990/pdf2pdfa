"""Owned TrueType ``glyf`` outline decoder and PDF glyph path builder."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .raster import Matrix, Path
from .ttf import FontParseError, SFNTFont


class TrueTypeOutlineError(FontParseError):
    pass


@dataclass(frozen=True, slots=True)
class TTPoint:
    x: float
    y: float
    on_curve: bool


@dataclass(frozen=True, slots=True)
class TTGlyph:
    contours: tuple[tuple[TTPoint, ...], ...]
    bbox: tuple[int, int, int, int]


_ARG_1_AND_2_ARE_WORDS = 0x0001
_ARGS_ARE_XY_VALUES = 0x0002
_ROUND_XY_TO_GRID = 0x0004
_WE_HAVE_A_SCALE = 0x0008
_MORE_COMPONENTS = 0x0020
_WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
_WE_HAVE_A_TWO_BY_TWO = 0x0080
_WE_HAVE_INSTRUCTIONS = 0x0100
_USE_MY_METRICS = 0x0200
_OVERLAP_COMPOUND = 0x0400
_SCALED_COMPONENT_OFFSET = 0x0800
_UNSCALED_COMPONENT_OFFSET = 0x1000


class TrueTypeOutlines:
    def __init__(self, font: SFNTFont) -> None:
        if not font.is_truetype:
            raise TrueTypeOutlineError("TrueType glyf outlines are required")
        self.font = font
        self._glyf = self._table("glyf")
        self._loca = self._offsets()
        self._cache: dict[int, TTGlyph] = {}

    def _table(self, tag: str) -> bytes:
        try:
            record = self.font.tables[tag]
        except KeyError as exc:
            raise TrueTypeOutlineError(f"font is missing {tag!r} table") from exc
        return self.font.data[record.offset : record.offset + record.length]

    @staticmethod
    def _u16(data: bytes, offset: int) -> int:
        if offset + 2 > len(data):
            raise TrueTypeOutlineError("TrueType glyph data is truncated")
        return int.from_bytes(data[offset : offset + 2], "big")

    @staticmethod
    def _i16(data: bytes, offset: int) -> int:
        if offset + 2 > len(data):
            raise TrueTypeOutlineError("TrueType glyph data is truncated")
        return int.from_bytes(data[offset : offset + 2], "big", signed=True)

    @staticmethod
    def _f2dot14(data: bytes, offset: int) -> float:
        return TrueTypeOutlines._i16(data, offset) / 16384.0

    def _offsets(self) -> tuple[int, ...]:
        head = self._table("head")
        if len(head) < 54:
            raise TrueTypeOutlineError("head table is truncated")
        fmt = self._i16(head, 50)
        loca = self._table("loca")
        count = self.font.glyph_count + 1
        offsets: list[int] = []
        if fmt == 0:
            if len(loca) < count * 2:
                raise TrueTypeOutlineError("short loca table is truncated")
            offsets = [self._u16(loca, index * 2) * 2 for index in range(count)]
        elif fmt == 1:
            if len(loca) < count * 4:
                raise TrueTypeOutlineError("long loca table is truncated")
            offsets = [int.from_bytes(loca[index * 4 : index * 4 + 4], "big") for index in range(count)]
        else:
            raise TrueTypeOutlineError(f"unsupported indexToLocFormat {fmt}")
        if any(offset < 0 or offset > len(self._glyf) for offset in offsets):
            raise TrueTypeOutlineError("loca offset points outside glyf table")
        if any(right < left for left, right in zip(offsets, offsets[1:])):
            raise TrueTypeOutlineError("loca offsets are not monotonic")
        return tuple(offsets)

    def glyph(self, glyph_id: int) -> TTGlyph:
        if glyph_id in self._cache:
            return self._cache[glyph_id]
        if not 0 <= glyph_id < self.font.glyph_count:
            raise TrueTypeOutlineError(f"glyph id {glyph_id} is outside font")
        start, end = self._loca[glyph_id], self._loca[glyph_id + 1]
        if start == end:
            glyph = TTGlyph((), (0, 0, 0, 0))
        else:
            data = self._glyf[start:end]
            if len(data) < 10:
                raise TrueTypeOutlineError(f"glyph {glyph_id} header is truncated")
            contour_count = self._i16(data, 0)
            bbox = (
                self._i16(data, 2),
                self._i16(data, 4),
                self._i16(data, 6),
                self._i16(data, 8),
            )
            if contour_count >= 0:
                glyph = TTGlyph(self._simple(data, contour_count), bbox)
            else:
                glyph = TTGlyph(self._composite(data, glyph_id, depth=0), bbox)
        self._cache[glyph_id] = glyph
        return glyph

    def _simple(self, data: bytes, contour_count: int) -> tuple[tuple[TTPoint, ...], ...]:
        position = 10
        if contour_count == 0:
            return ()
        if position + contour_count * 2 + 2 > len(data):
            raise TrueTypeOutlineError("simple glyph contour endpoints are truncated")
        end_points = [self._u16(data, position + index * 2) for index in range(contour_count)]
        position += contour_count * 2
        if any(right <= left for left, right in zip([-1, *end_points[:-1]], end_points)):
            raise TrueTypeOutlineError("simple glyph contour endpoints are invalid")
        point_count = end_points[-1] + 1
        instruction_length = self._u16(data, position)
        position += 2
        if position + instruction_length > len(data):
            raise TrueTypeOutlineError("simple glyph instructions are truncated")
        position += instruction_length

        flags: list[int] = []
        while len(flags) < point_count:
            if position >= len(data):
                raise TrueTypeOutlineError("simple glyph flags are truncated")
            flag = data[position]
            position += 1
            flags.append(flag)
            if flag & 0x08:
                if position >= len(data):
                    raise TrueTypeOutlineError("simple glyph repeated flag is truncated")
                repeat = data[position]
                position += 1
                flags.extend([flag] * repeat)
        if len(flags) != point_count:
            raise TrueTypeOutlineError("simple glyph flag repeat exceeds point count")

        xs: list[int] = []
        x = 0
        for flag in flags:
            if flag & 0x02:
                if position >= len(data):
                    raise TrueTypeOutlineError("simple glyph x coordinate is truncated")
                delta = data[position]
                position += 1
                x += delta if flag & 0x10 else -delta
            elif not flag & 0x10:
                x += self._i16(data, position)
                position += 2
            xs.append(x)

        ys: list[int] = []
        y = 0
        for flag in flags:
            if flag & 0x04:
                if position >= len(data):
                    raise TrueTypeOutlineError("simple glyph y coordinate is truncated")
                delta = data[position]
                position += 1
                y += delta if flag & 0x20 else -delta
            elif not flag & 0x20:
                y += self._i16(data, position)
                position += 2
            ys.append(y)

        points = [
            TTPoint(float(xs[index]), float(ys[index]), bool(flags[index] & 0x01))
            for index in range(point_count)
        ]
        contours: list[tuple[TTPoint, ...]] = []
        start = 0
        for end in end_points:
            contours.append(tuple(points[start : end + 1]))
            start = end + 1
        return tuple(contours)

    def _composite(self, data: bytes, glyph_id: int, *, depth: int) -> tuple[tuple[TTPoint, ...], ...]:
        if depth > 32:
            raise TrueTypeOutlineError("composite glyph recursion is too deep")
        position = 10
        output: list[tuple[TTPoint, ...]] = []
        flags = _MORE_COMPONENTS
        while flags & _MORE_COMPONENTS:
            if position + 4 > len(data):
                raise TrueTypeOutlineError("composite glyph component header is truncated")
            flags = self._u16(data, position)
            component_gid = self._u16(data, position + 2)
            position += 4
            if flags & _ARG_1_AND_2_ARE_WORDS:
                if position + 4 > len(data):
                    raise TrueTypeOutlineError("composite glyph component arguments are truncated")
                arg1 = self._i16(data, position)
                arg2 = self._i16(data, position + 2)
                position += 4
            else:
                if position + 2 > len(data):
                    raise TrueTypeOutlineError("composite glyph byte arguments are truncated")
                arg1 = int.from_bytes(data[position : position + 1], "big", signed=True)
                arg2 = int.from_bytes(data[position + 1 : position + 2], "big", signed=True)
                position += 2

            a = d = 1.0
            b = c = 0.0
            if flags & _WE_HAVE_A_SCALE:
                a = d = self._f2dot14(data, position)
                position += 2
            elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
                a = self._f2dot14(data, position)
                d = self._f2dot14(data, position + 2)
                position += 4
            elif flags & _WE_HAVE_A_TWO_BY_TWO:
                a = self._f2dot14(data, position)
                b = self._f2dot14(data, position + 2)
                c = self._f2dot14(data, position + 4)
                d = self._f2dot14(data, position + 6)
                position += 8

            component = self.glyph(component_gid)
            if flags & _ARGS_ARE_XY_VALUES:
                dx, dy = float(arg1), float(arg2)
                if flags & _ROUND_XY_TO_GRID:
                    dx, dy = round(dx), round(dy)
            else:
                # Point-to-point alignment. The first point belongs to the
                # already assembled parent outline, the second to the component.
                parent_points = [point for contour in output for point in contour]
                child_points = [point for contour in component.contours for point in contour]
                if not (0 <= arg1 < len(parent_points) and 0 <= arg2 < len(child_points)):
                    raise TrueTypeOutlineError("composite glyph point alignment index is invalid")
                parent_point = parent_points[arg1]
                child_point = child_points[arg2]
                transformed_child = (
                    a * child_point.x + c * child_point.y,
                    b * child_point.x + d * child_point.y,
                )
                dx = parent_point.x - transformed_child[0]
                dy = parent_point.y - transformed_child[1]

            for contour in component.contours:
                transformed = tuple(
                    TTPoint(
                        a * point.x + c * point.y + dx,
                        b * point.x + d * point.y + dy,
                        point.on_curve,
                    )
                    for point in contour
                )
                output.append(transformed)

        if flags & _WE_HAVE_INSTRUCTIONS:
            if position + 2 > len(data):
                raise TrueTypeOutlineError("composite glyph instruction length is truncated")
            length = self._u16(data, position)
            position += 2
            if position + length > len(data):
                raise TrueTypeOutlineError("composite glyph instructions are truncated")
        return tuple(output)

    def path(self, glyph_id: int, transform: Matrix = Matrix()) -> Path:
        glyph = self.glyph(glyph_id)
        result = Path()
        for contour in glyph.contours:
            _append_quadratic_contour(result, contour, transform)
        return result


def _point_mid(a: TTPoint, b: TTPoint) -> TTPoint:
    return TTPoint((a.x + b.x) * 0.5, (a.y + b.y) * 0.5, True)


def _append_quadratic_contour(path: Path, contour: tuple[TTPoint, ...], transform: Matrix) -> None:
    if not contour:
        return
    points = list(contour)
    first, last = points[0], points[-1]
    if first.on_curve:
        start = first
        index = 1
    elif last.on_curve:
        start = last
        index = 0
    else:
        start = _point_mid(last, first)
        index = 0
    path.move_to(*transform.transform(start.x, start.y))
    current = start
    consumed = 0
    count = len(points)
    while consumed < count:
        point = points[index % count]
        next_point = points[(index + 1) % count]
        if point.on_curve:
            path.line_to(*transform.transform(point.x, point.y))
            current = point
            index += 1
            consumed += 1
            continue
        if next_point.on_curve:
            end = next_point
            index += 2
            consumed += 2
        else:
            end = _point_mid(point, next_point)
            index += 1
            consumed += 1
        # Convert quadratic P0-P1-P2 to cubic exactly.
        c1 = (
            current.x + (2.0 / 3.0) * (point.x - current.x),
            current.y + (2.0 / 3.0) * (point.y - current.y),
        )
        c2 = (
            end.x + (2.0 / 3.0) * (point.x - end.x),
            end.y + (2.0 / 3.0) * (point.y - end.y),
        )
        tc1 = transform.transform(*c1)
        tc2 = transform.transform(*c2)
        tend = transform.transform(end.x, end.y)
        path.curve_to(*tc1, *tc2, *tend, tolerance=0.2)
        current = end
    path.close()
