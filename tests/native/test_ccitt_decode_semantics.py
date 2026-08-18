from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.owned_renderer import render_page_full


def _bits(value: str) -> bytes:
    clean = "".join(value.split())
    clean += "0" * ((-len(clean)) % 8)
    return bytes(int(clean[i : i + 8], 2) for i in range(0, len(clean), 8))


def _pdf() -> bytes:
    builder = PDFBuilder(version="1.7")
    # Fax coding itself is white4/black4. BlackIs1 reverses the filter's output
    # bit convention; /Decode [1 0] then reverses ordinary DeviceGray mapping.
    # The materialized-filter adapter must normalize only the filter convention,
    # not discard the independent Image /Decode mapping.
    image = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Image"),
                    "Width": 8,
                    "Height": 1,
                    "BitsPerComponent": 1,
                    "ColorSpace": PDFName("DeviceGray"),
                    "Filter": PDFName("CCITTFaxDecode"),
                    "DecodeParms": PDFDict(
                        {"K": 0, "Columns": 8, "Rows": 1, "BlackIs1": True}
                    ),
                    "Decode": [1, 0],
                }
            ),
            _bits("1011 011"),
        )
    )
    content = builder.add(
        PDFStream(PDFDict(), b"80 0 0 10 0 0 cm /Im Do\n")
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 80, 10],
                "Resources": PDFDict({"XObject": PDFDict({"Im": image})}),
                "Contents": content,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def _pixel(page, x: int):
    return page.surface.get_pixel(x, page.height - 1 - 5)


def test_blackis1_normalization_preserves_independent_decode_array():
    page = render_page_full(_pdf(), dpi=72)
    left = _pixel(page, 10)
    right = _pixel(page, 70)
    # /Decode [1 0] inverts the normalized ordinary bilevel image: the original
    # white half becomes black and the original black half becomes white.
    assert left.r < 0.02 and left.g < 0.02 and left.b < 0.02
    assert right.r > 0.98 and right.g > 0.98 and right.b > 0.98
