"""Affine-correct PDF stroke outlining in pure Python.

PDF line width, dash lengths, caps and joins are defined in user space. This
module reconstructs a flattened device path back into the path CTM's user
space, creates the stroked outline there, then transforms that filled outline
back to device space. It therefore avoids the incorrect scalar-width shortcut
under non-uniform scale or skew.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .raster import Color, Matrix, Path, Surface


class StrokeError(ValueError):
    pass


_EPS = 1e-9


def inverse(matrix: Matrix) -> Matrix:
    determinant = matrix.a * matrix.d - matrix.b * matrix.c
    if abs(determinant) < 1e-15:
        raise StrokeError("stroke CTM is singular")
    a = matrix.d / determinant
    b = -matrix.b / determinant
    c = -matrix.c / determinant
    d = matrix.a / determinant
    e = -(a * matrix.e + c * matrix.f)
    f = -(b * matrix.e + d * matrix.f)
    return Matrix(a, b, c, d, e, f)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _lerp(a, b, t: float):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _normalize(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length <= _EPS:
        raise StrokeError("zero-length direction")
    return dx / length, dy / length


def _normal(direction: tuple[float, float]) -> tuple[float, float]:
    return -direction[1], direction[0]


def _offset(point, vector, amount):
    return point[0] + vector[0] * amount, point[1] + vector[1] * amount


def _line_intersection(p, d, q, e) -> tuple[float, float] | None:
    cross = d[0] * e[1] - d[1] * e[0]
    if abs(cross) <= _EPS:
        return None
    qp = (q[0] - p[0], q[1] - p[1])
    t = (qp[0] * e[1] - qp[1] * e[0]) / cross
    return p[0] + t * d[0], p[1] + t * d[1]


def _polygon(path: Path, points: list[tuple[float, float]]) -> None:
    if len(points) < 3:
        return
    path.move_to(*points[0])
    for point in points[1:]:
        path.line_to(*point)
    path.close()


def _circle(path: Path, center, radius: float, *, steps: int = 20) -> None:
    points = [
        (
            center[0] + math.cos(2 * math.pi * index / steps) * radius,
            center[1] + math.sin(2 * math.pi * index / steps) * radius,
        )
        for index in range(steps)
    ]
    _polygon(path, points)


def _segment_quad(
    path: Path,
    start,
    end,
    radius: float,
    *,
    start_extend: float = 0.0,
    end_extend: float = 0.0,
) -> None:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if math.hypot(dx, dy) <= _EPS:
        return
    direction = _normalize(dx, dy)
    normal = _normal(direction)
    s = _offset(start, direction, -start_extend)
    e = _offset(end, direction, end_extend)
    _polygon(
        path,
        [
            _offset(s, normal, radius),
            _offset(e, normal, radius),
            _offset(e, normal, -radius),
            _offset(s, normal, -radius),
        ],
    )


def _join(
    path: Path,
    previous,
    vertex,
    following,
    radius: float,
    *,
    line_join: int,
    miter_limit: float,
) -> None:
    d1_raw = (vertex[0] - previous[0], vertex[1] - previous[1])
    d2_raw = (following[0] - vertex[0], following[1] - vertex[1])
    if math.hypot(*d1_raw) <= _EPS or math.hypot(*d2_raw) <= _EPS:
        return
    d1 = _normalize(*d1_raw)
    d2 = _normalize(*d2_raw)
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) <= _EPS:
        return
    if line_join == 1:
        _circle(path, vertex, radius)
        return
    side = 1.0 if cross > 0 else -1.0
    n1 = _normal(d1)
    n2 = _normal(d2)
    outer1 = _offset(vertex, n1, radius * side)
    outer2 = _offset(vertex, n2, radius * side)
    if line_join == 2:
        _polygon(path, [outer1, outer2, vertex])
        return
    intersection = _line_intersection(outer1, d1, outer2, d2)
    if intersection is None:
        _polygon(path, [outer1, outer2, vertex])
        return
    miter_length = _distance(vertex, intersection)
    if radius <= _EPS or miter_length / radius > miter_limit:
        _polygon(path, [outer1, outer2, vertex])
    else:
        _polygon(path, [outer1, intersection, outer2, vertex])


def _stroke_polyline(
    outline: Path,
    points: list[tuple[float, float]],
    *,
    closed: bool,
    radius: float,
    line_cap: int,
    line_join: int,
    miter_limit: float,
) -> None:
    points = [point for index, point in enumerate(points) if index == 0 or _distance(points[index - 1], point) > _EPS]
    if len(points) < 2:
        if points and line_cap == 1:
            _circle(outline, points[0], radius)
        return
    count = len(points) if closed else len(points) - 1
    for index in range(count):
        start = points[index]
        end = points[(index + 1) % len(points)]
        start_extend = radius if (not closed and index == 0 and line_cap == 2) else 0.0
        end_extend = radius if (not closed and index == count - 1 and line_cap == 2) else 0.0
        _segment_quad(
            outline,
            start,
            end,
            radius,
            start_extend=start_extend,
            end_extend=end_extend,
        )
    if closed:
        for index in range(len(points)):
            _join(
                outline,
                points[index - 1],
                points[index],
                points[(index + 1) % len(points)],
                radius,
                line_join=line_join,
                miter_limit=miter_limit,
            )
    else:
        for index in range(1, len(points) - 1):
            _join(
                outline,
                points[index - 1],
                points[index],
                points[index + 1],
                radius,
                line_join=line_join,
                miter_limit=miter_limit,
            )
        if line_cap == 1:
            _circle(outline, points[0], radius)
            _circle(outline, points[-1], radius)


def _normalized_dash(pattern: tuple[float, ...], phase: float) -> tuple[tuple[float, ...], float]:
    if not pattern:
        return (), 0.0
    if any(value < 0 for value in pattern) or sum(pattern) <= _EPS:
        raise StrokeError("invalid dash pattern")
    if len(pattern) % 2:
        pattern = pattern + pattern
    total = sum(pattern)
    phase %= total
    return pattern, phase


def _dash_fragments(
    points: list[tuple[float, float]],
    *,
    closed: bool,
    pattern: tuple[float, ...],
    phase: float,
) -> list[list[tuple[float, float]]]:
    if len(points) < 2:
        return []
    pattern, phase = _normalized_dash(pattern, phase)
    if not pattern:
        return [points]
    sequence = list(points)
    if closed:
        sequence.append(points[0])
    index = 0
    while phase >= pattern[index] - _EPS:
        phase -= pattern[index]
        index = (index + 1) % len(pattern)
    remaining_dash = pattern[index] - phase
    on = index % 2 == 0
    fragments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] | None = [sequence[0]] if on else None

    for segment_start, segment_end in zip(sequence, sequence[1:]):
        segment_length = _distance(segment_start, segment_end)
        if segment_length <= _EPS:
            continue
        consumed = 0.0
        cursor = segment_start
        while consumed < segment_length - _EPS:
            step = min(remaining_dash, segment_length - consumed)
            next_point = _lerp(segment_start, segment_end, (consumed + step) / segment_length)
            if on:
                if current is None:
                    current = [cursor]
                current.append(next_point)
            consumed += step
            cursor = next_point
            remaining_dash -= step
            if remaining_dash <= _EPS:
                if on and current is not None and len(current) >= 2:
                    fragments.append(current)
                    current = None
                index = (index + 1) % len(pattern)
                on = index % 2 == 0
                remaining_dash = pattern[index]
                if on:
                    current = [cursor]
    if on and current is not None and len(current) >= 2:
        fragments.append(current)

    if closed and len(fragments) >= 2:
        # If dash state was on across the path seam, first/last fragments are one
        # continuous dash and shall be joined instead of capped twice.
        if _distance(fragments[0][0], points[0]) <= _EPS and _distance(fragments[-1][-1], points[0]) <= _EPS:
            merged = fragments[-1][:-1] + fragments[0]
            fragments = [merged, *fragments[1:-1]]
    return fragments


def _device_to_user_path(path: Path, path_ctm: Matrix) -> Path:
    inv = inverse(path_ctm)
    user = Path()
    for subpath in path.subpaths:
        if not subpath.points:
            continue
        user.move_to(*inv.transform(*subpath.points[0]))
        for point in subpath.points[1:]:
            user.line_to(*inv.transform(*point))
        if subpath.closed:
            user.close()
    return user


def stroke_affine(
    surface: Surface,
    device_path: Path,
    *,
    path_ctm: Matrix,
    line_width: float,
    line_cap: int,
    line_join: int,
    miter_limit: float,
    dash_array: tuple[float, ...],
    dash_phase: float,
    color: Color,
    blend_mode: str,
) -> None:
    if line_cap not in (0, 1, 2) or line_join not in (0, 1, 2):
        raise StrokeError("invalid line cap/join")
    if miter_limit < 1:
        raise StrokeError("miter limit shall be at least 1")
    if line_width < 0:
        raise StrokeError("line width shall be non-negative")
    if line_width == 0:
        # PDF hairline: approximately one device pixel regardless of CTM.
        surface.stroke_path(
            device_path,
            color,
            stroke_width=1.0,
            line_cap=line_cap,
            line_join=line_join,
            blend_mode=blend_mode,
        )
        return

    user_path = _device_to_user_path(device_path, path_ctm)
    outline = Path()
    radius = line_width * 0.5
    for subpath in user_path.subpaths:
        if len(subpath.points) < 2:
            continue
        if dash_array:
            fragments = _dash_fragments(
                subpath.points,
                closed=subpath.closed,
                pattern=dash_array,
                phase=dash_phase,
            )
            for fragment in fragments:
                _stroke_polyline(
                    outline,
                    fragment,
                    closed=False,
                    radius=radius,
                    line_cap=line_cap,
                    line_join=line_join,
                    miter_limit=miter_limit,
                )
        else:
            _stroke_polyline(
                outline,
                subpath.points,
                closed=subpath.closed,
                radius=radius,
                line_cap=line_cap,
                line_join=line_join,
                miter_limit=miter_limit,
            )
    device_outline = outline.transformed(path_ctm)
    surface.fill_path(device_outline, color, blend_mode=blend_mode)
