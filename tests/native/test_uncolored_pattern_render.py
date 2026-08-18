from __future__ import annotations

import pytest

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer, render_page_full
from pdf2pdfa.native.pattern_render import PatternRenderError, UnsupportedPatternError
from pdf2pdfa.native.uncolored_pattern import UncoloredTilingPatternRendererMixin


def _pattern(
    *,
    content: bytes = b"0 0 5 10 re f\n",
    resources: PDFDict | None = None,
    x_step: float = 10,
    y_step: float = 10,
    bbox=(0, 0, 10, 10),
) -> PDFStream:
    return PDFStream(
        PDFDict(
            {
                "Type": PDFName("Pattern"),
                "PatternType": 1,
                "PaintType": 2,
                "TilingType": 1,
                "BBox": list(bbox),
                "XStep": x_step,
                "YStep": y_step,
                "Resources": resources or PDFDict(),
            }
        ),
        content,
    )


def _pdf(
    pattern: PDFStream,
    *,
    base_space=PDFName("DeviceRGB"),
    components=(1, 0, 0),
    width=40,
    height=20,
    prefix: bytes = b"",
) -> bytes:
    builder = PDFBuilder(version="1.7")
    pattern_ref = builder.add(pattern)
    resources = PDFDict(
        {
            "Pattern": PDFDict({"P": pattern_ref}),
            "ColorSpace": PDFDict({"UC": [PDFName("Pattern"), base_space]}),
        }
    )
    component_text = " ".join(str(value) for value in components).encode("ascii")
    content = (
        prefix
        + b"/UC cs "
        + component_text
        + b" /P scn 0 0 "
        + str(width).encode("ascii")
        + b" "
        + str(height).encode("ascii")
        + b" re f\n"
    )
    contents = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, width, height],
                "Resources": resources,
                "Contents": contents,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int, y: int = 5):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_full_renderer_mro_includes_uncolored_tiling_layer():
    assert UncoloredTilingPatternRendererMixin in FullOwnedPageRenderer.__mro__


def test_uncolored_pattern_uses_external_rgb_and_repeats_shape():
    red = render_page_full(_pdf(_pattern(), components=(1, 0, 0)), dpi=72)
    blue = render_page_full(_pdf(_pattern(), components=(0, 0, 1)), dpi=72)
    for page, channel in ((red, "r"), (blue, "b")):
        painted = _pixel(page, 2)
        gap = _pixel(page, 7)
        repeated = _pixel(page, 12)
        assert getattr(painted, channel) > 0.98
        assert getattr(repeated, channel) > 0.98
        assert gap.r > 0.99 and gap.g > 0.99 and gap.b > 0.99
    assert red.rgb_bytes() != blue.rgb_bytes()


def test_uncolored_pattern_supports_devicegray_base_space():
    page = render_page_full(
        _pdf(_pattern(), base_space=PDFName("DeviceGray"), components=(0.25,)),
        dpi=72,
    )
    painted = _pixel(page, 2)
    assert 0.22 < painted.r < 0.28
    assert abs(painted.r - painted.g) < 0.01
    assert abs(painted.g - painted.b) < 0.01


def test_uncolored_pattern_supports_devicecmyk_base_space():
    page = render_page_full(
        _pdf(
            _pattern(),
            base_space=PDFName("DeviceCMYK"),
            components=(1, 0, 0, 0),
        ),
        dpi=72,
    )
    cyan = _pixel(page, 2)
    assert cyan.r < 0.05 and cyan.g > 0.95 and cyan.b > 0.95


def test_cell_alpha_shapes_external_color_once():
    gs = PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5})
    pattern = _pattern(
        content=b"/GS gs 0 0 5 10 re f\n",
        resources=PDFDict({"ExtGState": PDFDict({"GS": gs})}),
    )
    page = render_page_full(_pdf(pattern, components=(1, 0, 0)), dpi=72)
    painted = _pixel(page, 2)
    assert painted.r > 0.98
    assert 0.45 < painted.g < 0.55 and 0.45 < painted.b < 0.55


def test_uncolored_pattern_allows_image_mask_as_shape():
    mask = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 1,
                "Height": 1,
                "ImageMask": True,
                "BitsPerComponent": 1,
            }
        ),
        b"\x00",  # default Decode: zero paints for a stencil mask
    )
    pattern = _pattern(
        content=b"5 0 0 10 0 0 cm /Im Do\n",
        resources=PDFDict({"XObject": PDFDict({"Im": mask})}),
    )
    page = render_page_full(_pdf(pattern, components=(0, 1, 0)), dpi=72)
    green = _pixel(page, 2)
    gap = _pixel(page, 7)
    assert green.g > 0.98 and green.r < 0.05 and green.b < 0.05
    assert gap.r > 0.99 and gap.g > 0.99 and gap.b > 0.99


def test_uncolored_pattern_rejects_color_operator_inside_cell():
    pattern = _pattern(content=b"1 0 0 rg 0 0 5 10 re f\n")
    with pytest.raises(UnsupportedPatternError, match="operator rg"):
        render_page_full(_pdf(pattern), dpi=72)


def test_uncolored_pattern_rejects_intrinsic_color_image():
    image = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 1,
                "Height": 1,
                "BitsPerComponent": 8,
                "ColorSpace": PDFName("DeviceRGB"),
            }
        ),
        b"\xff\x00\x00",
    )
    pattern = _pattern(
        content=b"5 0 0 10 0 0 cm /Im Do\n",
        resources=PDFDict({"XObject": PDFDict({"Im": image})}),
    )
    with pytest.raises(UnsupportedPatternError, match="intrinsic-color Image"):
        render_page_full(_pdf(pattern), dpi=72)


def test_uncolored_pattern_rejects_shading_inside_cell():
    shading = PDFDict(
        {
            "ShadingType": 2,
            "ColorSpace": PDFName("DeviceRGB"),
            "Coords": [0, 0, 10, 0],
            "Function": PDFDict(
                {
                    "FunctionType": 2,
                    "Domain": [0, 1],
                    "C0": [1, 0, 0],
                    "C1": [0, 0, 1],
                    "N": 1,
                }
            ),
        }
    )
    pattern = _pattern(
        content=b"/Sh sh\n",
        resources=PDFDict({"Shading": PDFDict({"Sh": shading})}),
    )
    with pytest.raises(UnsupportedPatternError, match="intrinsically colored shading"):
        render_page_full(_pdf(pattern), dpi=72)


def test_wrong_base_component_count_is_rejected_before_painting():
    with pytest.raises(PatternRenderError, match="expects 3 base component"):
        render_page_full(_pdf(_pattern(), components=(1, 0)), dpi=72)
