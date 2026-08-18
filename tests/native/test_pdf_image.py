from __future__ import annotations

import zlib

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdf_image import decode_pdf_image
from tests.native.test_jpeg import minimal_gray_jpeg


def _doc_with_stream(stream: PDFStream) -> tuple[PDFDocument, object]:
    builder = PDFBuilder(version="1.7")
    ref = builder.add(stream)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 10, 10],
                "Resources": PDFDict(),
            }
        )
    )
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False), ref


def test_raw_rgb_image_decodes_exact_pixels():
    stream = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 2,
                "Height": 1,
                "BitsPerComponent": 8,
                "ColorSpace": PDFName("DeviceRGB"),
            }
        ),
        bytes([255, 0, 0, 0, 255, 0]),
    )
    doc, ref = _doc_with_stream(stream)
    image = decode_pdf_image(doc, ref)
    assert image.rgba == bytes([255, 0, 0, 255, 0, 255, 0, 255])


def test_flate_one_bit_grayscale_samples_unpack_msb_first():
    raw = bytes([0b01010000])
    stream = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 4,
                "Height": 1,
                "BitsPerComponent": 1,
                "ColorSpace": PDFName("DeviceGray"),
                "Filter": PDFName("FlateDecode"),
            }
        ),
        zlib.compress(raw),
    )
    doc, ref = _doc_with_stream(stream)
    image = decode_pdf_image(doc, ref)
    assert [image.rgba[index] for index in range(0, len(image.rgba), 4)] == [0, 255, 0, 255]


def test_indexed_default_decode_uses_sample_index_then_clamps_hival():
    # With bpc=8 and hival=1, sample 128 must clamp to palette index 1,
    # not scale to ~0.5 and round ambiguously.
    stream = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 2,
                "Height": 1,
                "BitsPerComponent": 8,
                "ColorSpace": [
                    PDFName("Indexed"),
                    PDFName("DeviceRGB"),
                    1,
                    bytes([255, 0, 0, 0, 0, 255]),
                ],
            }
        ),
        bytes([0, 128]),
    )
    doc, ref = _doc_with_stream(stream)
    image = decode_pdf_image(doc, ref)
    assert image.rgba == bytes([255, 0, 0, 255, 0, 0, 255, 255])


def test_explicit_image_mask_uses_mask_alpha_not_white_luminance():
    builder = PDFBuilder(version="1.7")
    mask_ref = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Image"),
                    "Width": 2,
                    "Height": 1,
                    "ImageMask": True,
                    "BitsPerComponent": 1,
                }
            ),
            bytes([0b01000000]),
        )
    )
    image_ref = builder.add(
        PDFStream(
            PDFDict(
                {
                    "Type": PDFName("XObject"),
                    "Subtype": PDFName("Image"),
                    "Width": 2,
                    "Height": 1,
                    "BitsPerComponent": 8,
                    "ColorSpace": PDFName("DeviceRGB"),
                    "Mask": mask_ref,
                }
            ),
            bytes([255, 0, 0, 255, 0, 0]),
        )
    )
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(PDFDict({"Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,10,10]}))
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    image = decode_pdf_image(doc, image_ref)
    assert image.rgba[3] == 255
    assert image.rgba[7] == 0


def test_dctdecode_uses_owned_jpeg_decoder():
    stream = PDFStream(
        PDFDict(
            {
                "Type": PDFName("XObject"),
                "Subtype": PDFName("Image"),
                "Width": 8,
                "Height": 8,
                "BitsPerComponent": 8,
                "ColorSpace": PDFName("DeviceGray"),
                "Filter": PDFName("DCTDecode"),
            }
        ),
        minimal_gray_jpeg(),
    )
    doc, ref = _doc_with_stream(stream)
    image = decode_pdf_image(doc, ref)
    assert image.width == 8 and image.height == 8
    assert image.rgba[:4] == bytes([128, 128, 128, 255])
