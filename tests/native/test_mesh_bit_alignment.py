from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full


def _pack(bits: str) -> bytes:
    clean = "".join(bits.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


def _record4(flag: int, x: int, y: int, r: int, g: int, b: int, *, pad: str = "00") -> str:
    # 2 + 2*4 + 3*4 = 22 payload bits; Type4 then aligns each record.
    return (
        f"{flag:02b}{x:04b}{y:04b}{r:04b}{g:04b}{b:04b}" + pad
    )


def _record5(x: int, y: int, r: int, g: int, b: int) -> str:
    # Type5 is continuous: 2*4 + 3*4 = 20 bits, no per-vertex padding.
    return f"{x:04b}{y:04b}{r:04b}{g:04b}{b:04b}"


def _pdf(shading: PDFStream) -> bytes:
    builder = PDFBuilder(version="1.7")
    shading_ref = builder.add(shading)
    content = builder.add(PDFStream(PDFDict(), b"/Sh sh\n"))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 100, 100],
                "Resources": PDFDict({"Shading": PDFDict({"Sh": shading_ref})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _bottom_pixel(page, x: int, y: int):
    return page.surface.get_pixel(x, page.height - 1 - y)


def test_type4_aligns_after_every_vertex_and_ignores_noncontrol_flags():
    bits = "".join(
        [
            _record4(0, 0, 0, 15, 0, 0, pad="11"),
            # Flags 3 are invalid as topology controls, but these two records
            # are the pending vertices of a flag-0 triangle and their flag
            # fields are semantically ignored.
            _record4(3, 15, 0, 0, 15, 0, pad="10"),
            _record4(3, 0, 15, 0, 0, 15, pad="01"),
            _record4(1, 15, 15, 15, 15, 15, pad="11"),
        ]
    )
    shading = PDFStream(
        PDFDict(
            {
                "ShadingType": 4,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerCoordinate": 4,
                "BitsPerComponent": 4,
                "BitsPerFlag": 2,
                "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
            }
        ),
        _pack(bits),
    )
    page = render_page_full(_pdf(shading), dpi=72)
    assert _bottom_pixel(page, 10, 10).r > 0.7
    upper = _bottom_pixel(page, 90, 90)
    assert upper.r > 0.7 and upper.g > 0.7 and upper.b > 0.7


def test_type5_vertices_remain_bit_contiguous_without_record_alignment():
    bits = "".join(
        [
            _record5(0, 0, 15, 0, 0),
            _record5(15, 0, 0, 15, 0),
            _record5(0, 15, 0, 0, 15),
            _record5(15, 15, 15, 15, 15),
        ]
    )
    shading = PDFStream(
        PDFDict(
            {
                "ShadingType": 5,
                "ColorSpace": PDFName("DeviceRGB"),
                "BitsPerCoordinate": 4,
                "BitsPerComponent": 4,
                "VerticesPerRow": 2,
                "Decode": [0, 100, 0, 100, 0, 1, 0, 1, 0, 1],
            }
        ),
        _pack(bits),
    )
    page = render_page_full(_pdf(shading), dpi=72)
    assert _bottom_pixel(page, 5, 5).r > 0.8
    assert _bottom_pixel(page, 95, 5).g > 0.8
    assert _bottom_pixel(page, 5, 95).b > 0.8
