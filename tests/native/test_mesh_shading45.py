from __future__ import annotations

import zlib

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.page_render import RenderingError


def _v4(flag: int, x: int, y: int, r: int, g: int, b: int) -> bytes:
    return bytes([flag, x, y, r, g, b])


def _v5(x: int, y: int, r: int, g: int, b: int) -> bytes:
    return bytes([x, y, r, g, b])


def _mesh4(*, compressed: bool = False) -> PDFStream:
    # Two triangles covering the unit square. The fourth record uses edge flag
    # 1, reusing the previous triangle's second and third vertices.
    data = b"".join(
        [
            _v4(0, 0, 0, 255, 0, 0),
            _v4(0, 255, 0, 0, 255, 0),
            _v4(0, 0, 255, 0, 0, 255),
            _v4(1, 255, 255, 255, 255, 255),
        ]
    )
    dictionary = PDFDict(
        {
            "ShadingType": 4,
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


def _mesh5() -> PDFStream:
    data = b"".join(
        [
            _v5(0, 0, 255, 0, 0),
            _v5(255, 0, 0, 255, 0),
            _v5(0, 255, 0, 0, 255),
            _v5(255, 255, 255, 255, 255),
        ]
    )
    return PDFStream(
        PDFDict(
            {
                "ShadingType": 5,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerCoordinate": 8,
                "BitsPerComponent": 8,
                "VerticesPerRow": 2,
                "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
                "BBox": [0, 0, 100, 100],
            }
        ),
        data,
    )


def _pdf(
    shading: PDFStream,
    *,
    content: bytes = b"/Sh sh\n",
    pattern: bool = False,
    alpha: float | None = None,
) -> bytes:
    builder = PDFBuilder(version="1.7")
    shading_ref = builder.add(shading)
    resources = PDFDict()
    if pattern:
        pattern_ref = builder.add(
            PDFDict(
                {
                    "Type": PDFName("Pattern"),
                    "PatternType": 2,
                    "Shading": shading_ref,
                }
            )
        )
        resources["Pattern"] = PDFDict({"P": pattern_ref})
        content = b"/Pattern cs /P scn 0 0 100 100 re f\n"
    else:
        resources["Shading"] = PDFDict({"Sh": shading_ref})
    if alpha is not None:
        resources["ExtGState"] = PDFDict(
            {"GS": PDFDict({"Type": PDFName("ExtGState"), "ca": alpha})}
        )
        content = b"/GS gs " + content

    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": resources,
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_full_renderer_mro_contains_every_reachable_pattern_shading_layer():
    names = [cls.__name__ for cls in FullOwnedPageRenderer.__mro__]
    assert "ColoredTilingPatternRendererMixin" in names
    assert "CanonicalPatternShadingMixin" in names
    assert "PatternShadingRendererMixin" in names
    assert "ShadingRendererMixin" in names
    assert "TransparencyRenderer" in names


def test_type4_free_form_mesh_renders_both_flag_reuse_triangles():
    page = render_page_full(_pdf(_mesh4()), dpi=72)
    lower_left = _bottom_pixel(page, 15, 15)
    upper_right = _bottom_pixel(page, 85, 85)
    assert lower_left.r > lower_left.g and lower_left.r > lower_left.b
    assert upper_right.r > 0.65 and upper_right.g > 0.65 and upper_right.b > 0.65


def test_type5_lattice_mesh_interpolates_four_corner_colors():
    page = render_page_full(_pdf(_mesh5()), dpi=72)
    red = _bottom_pixel(page, 5, 5)
    green = _bottom_pixel(page, 95, 5)
    blue = _bottom_pixel(page, 5, 95)
    white = _bottom_pixel(page, 95, 95)
    assert red.r > 0.85 and red.g < 0.2 and red.b < 0.2
    assert green.g > 0.85 and green.r < 0.2 and green.b < 0.2
    assert blue.b > 0.85 and blue.r < 0.2 and blue.g < 0.2
    assert white.r > 0.85 and white.g > 0.85 and white.b > 0.85


def test_flate_filtered_mesh_stream_is_decoded_before_bit_parser():
    plain = render_page_full(_pdf(_mesh4(compressed=False)), dpi=72)
    compressed = render_page_full(_pdf(_mesh4(compressed=True)), dpi=72)
    assert plain.rgb_bytes() == compressed.rgb_bytes()


def test_pattern_type2_uses_same_mesh_dispatcher_as_direct_sh():
    direct = render_page_full(_pdf(_mesh5()), dpi=72)
    patterned = render_page_full(_pdf(_mesh5(), pattern=True), dpi=72)
    assert direct.rgb_bytes() == patterned.rgb_bytes()


def test_shared_mesh_diagonal_is_not_double_composited_with_alpha():
    page = render_page_full(_pdf(_mesh5(), alpha=0.5), dpi=72)
    # The center lies on the lattice diagonal. If both triangles owned the seam,
    # 50% alpha would be composited twice and the pixel would be too saturated.
    center = _bottom_pixel(page, 50, 50)
    assert center.r > 0.65 and center.g > 0.65 and center.b > 0.65


def test_mesh_respects_current_ctm():
    page = render_page_full(
        _pdf(_mesh5(), content=b"0.5 0 0 0.5 0 0 cm /Sh sh\n"),
        dpi=72,
    )
    painted = _bottom_pixel(page, 25, 25)
    outside = _bottom_pixel(page, 75, 75)
    assert min(painted.r, painted.g, painted.b) < 0.95
    assert outside.r > 0.99 and outside.g > 0.99 and outside.b > 0.99


def test_type4_invalid_reuse_flag_is_fail_closed():
    shading = _mesh4()
    data = bytearray(shading.data)
    data[18] = 3  # fourth record flag
    shading.data = bytes(data)
    with pytest.raises(RenderingError, match="edge flag 3"):
        render_page_full(_pdf(shading), dpi=72)
