from __future__ import annotations

import zlib

import pytest

from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full
from pdf2pdfa.native.page_render import RenderingError
from pdf2pdfa.native.patch_shading import (
    PatchPoint,
    _tensor_poles,
    decode_patch_mesh,
)

from tests.native.test_mesh_shading45 import _bottom_pixel, _pdf


BOUNDARY12 = [
    (0, 0), (85, 0), (170, 0), (255, 0),
    (255, 85), (255, 170), (255, 255),
    (170, 255), (85, 255), (0, 255),
    (0, 170), (0, 85),
]
INTERIOR4 = [(85, 85), (170, 85), (170, 170), (85, 170)]
COLORS4 = [
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 255),
    (0, 0, 255),
]


def _record(flag: int, points: list[tuple[int, int]], colors: list[tuple[int, int, int]]) -> bytes:
    payload = bytearray([flag])
    for x, y in points:
        payload.extend((x, y))
    for color in colors:
        payload.extend(color)
    return bytes(payload)


def _patch_stream(shading_type: int, *, compressed: bool = False) -> PDFStream:
    points = list(BOUNDARY12)
    if shading_type == 7:
        points += INTERIOR4
    data = _record(0, points, list(COLORS4))
    dictionary = PDFDict(
        {
            "ShadingType": shading_type,
            "ColorSpace": PDFName("DeviceRGB"),
            "BitsPerCoordinate": 8,
            "BitsPerComponent": 8,
            "BitsPerFlag": 8,
            "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
            "BBox": [0, 0, 100, 100],
        }
    )
    if compressed:
        dictionary["Filter"] = PDFName("FlateDecode")
        data = zlib.compress(data)
    return PDFStream(dictionary, data)


def test_type6_coons_rectangle_derives_bilinear_tensor_interior():
    points = [
        PatchPoint(0, 0), PatchPoint(1 / 3, 0), PatchPoint(2 / 3, 0), PatchPoint(1, 0),
        PatchPoint(1, 1 / 3), PatchPoint(1, 2 / 3), PatchPoint(1, 1),
        PatchPoint(2 / 3, 1), PatchPoint(1 / 3, 1), PatchPoint(0, 1),
        PatchPoint(0, 2 / 3), PatchPoint(0, 1 / 3),
    ]
    poles = _tensor_poles(6, points)
    expected = {
        (1, 1): (1 / 3, 1 / 3),
        (1, 2): (2 / 3, 1 / 3),
        (2, 2): (2 / 3, 2 / 3),
        (2, 1): (1 / 3, 2 / 3),
    }
    for (row, column), (x, y) in expected.items():
        assert poles[row][column].x == pytest.approx(x, abs=1e-12)
        assert poles[row][column].y == pytest.approx(y, abs=1e-12)


def test_type7_uses_explicit_four_interior_tensor_points():
    points = [PatchPoint(x / 255 * 100, y / 255 * 100) for x, y in BOUNDARY12 + INTERIOR4]
    poles = _tensor_poles(7, points)
    assert poles[1][1] == points[12]
    assert poles[1][2] == points[13]
    assert poles[2][2] == points[14]
    assert poles[2][1] == points[15]


@pytest.mark.parametrize("shading_type", [6, 7])
def test_patch_mesh_renders_four_corner_colors(shading_type: int):
    page = render_page_full(_pdf(_patch_stream(shading_type)), dpi=72)
    red = _bottom_pixel(page, 3, 3)
    green = _bottom_pixel(page, 96, 3)
    blue = _bottom_pixel(page, 3, 96)
    white = _bottom_pixel(page, 96, 96)
    assert red.r > 0.88 and red.g < 0.18 and red.b < 0.18
    assert green.g > 0.88 and green.r < 0.18 and green.b < 0.18
    assert blue.b > 0.88 and blue.r < 0.18 and blue.g < 0.18
    assert min(white.r, white.g, white.b) > 0.88


@pytest.mark.parametrize("shading_type", [6, 7])
def test_patch_mesh_is_reachable_through_pattern_type2(shading_type: int):
    direct = render_page_full(_pdf(_patch_stream(shading_type)), dpi=72)
    patterned = render_page_full(_pdf(_patch_stream(shading_type), pattern=True), dpi=72)
    assert direct.rgb_bytes() == patterned.rgb_bytes()


@pytest.mark.parametrize("shading_type", [6, 7])
def test_flate_patch_stream_is_decoded_before_patch_bit_parser(shading_type: int):
    plain = render_page_full(_pdf(_patch_stream(shading_type)), dpi=72)
    compressed = render_page_full(_pdf(_patch_stream(shading_type, compressed=True)), dpi=72)
    assert plain.rgb_bytes() == compressed.rgb_bytes()


def test_type6_reuse_flag_inherits_previous_edge_and_two_corner_colors():
    first = _record(0, list(BOUNDARY12), list(COLORS4))
    new_points = [
        (255, 255), (220, 220), (180, 180), (150, 150),
        (120, 120), (90, 90), (60, 60), (30, 30),
    ]
    second = _record(1, new_points, [(0, 0, 0), (255, 255, 0)])
    shading = _patch_stream(6)
    shading.data = first + second
    doc = PDFDocument.open(_pdf(shading), repair=True)
    _, patches = decode_patch_mesh(doc, shading)
    assert len(patches) == 2
    inherited = patches[1].poles[0]
    expected = BOUNDARY12[3:7]
    for pole, (x, y) in zip(inherited, expected):
        assert pole.x == pytest.approx(x / 255 * 100)
        assert pole.y == pytest.approx(y / 255 * 100)
    assert patches[1].colors[0] == pytest.approx((0, 1, 0))
    assert patches[1].colors[1] == pytest.approx((1, 1, 1))


def test_patch_reuse_before_initial_patch_is_fail_closed():
    shading = _patch_stream(6)
    shading.data = _record(1, [(0, 0)] * 8, [(0, 0, 0), (0, 0, 0)])
    with pytest.raises(RenderingError, match="reuse flag appears before an initial patch"):
        render_page_full(_pdf(shading), dpi=72)


def test_patch_invalid_flag_is_fail_closed_even_with_wide_flag_field():
    shading = _patch_stream(7)
    payload = bytearray(shading.data)
    payload[0] = 4
    shading.data = bytes(payload)
    with pytest.raises(RenderingError, match="edge flag 4"):
        render_page_full(_pdf(shading), dpi=72)


def test_patch_nonzero_trailing_bits_are_not_accepted_as_padding():
    shading = _patch_stream(6)
    shading.data += b"\x01"
    with pytest.raises(RenderingError, match="truncated patch mesh record"):
        render_page_full(_pdf(shading), dpi=72)
