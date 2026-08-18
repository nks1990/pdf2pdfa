from __future__ import annotations

import zlib

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.image import decode_image
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from tests.native.test_jpeg_v2 import minimal_gray_jpeg


def _doc(stream: PDFStream):
    builder = PDFBuilder(version="1.7")
    ref = builder.add(stream)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(PDFDict({"Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,10,10]}))
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return PDFDocument.open(builder.to_bytes(), repair=False), ref


def test_raw_rgb_pixels():
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceRGB"),
    }), bytes([255,0,0, 0,255,0])))
    assert decode_image(doc, ref).rgba == bytes([255,0,0,255, 0,255,0,255])


def test_flate_one_bit_gray_is_msb_first():
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 4, "Height": 1, "BitsPerComponent": 1,
        "ColorSpace": PDFName("DeviceGray"), "Filter": PDFName("FlateDecode"),
    }), zlib.compress(bytes([0b01010000]))))
    image = decode_image(doc, ref)
    assert [image.rgba[i] for i in range(0, len(image.rgba), 4)] == [0,255,0,255]


def test_indexed_default_decode_treats_sample_as_index_then_clamps_hival():
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 3, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": [PDFName("Indexed"), PDFName("DeviceRGB"), 1,
                       bytes([255,0,0, 0,0,255])],
    }), bytes([0, 1, 64])))
    image = decode_image(doc, ref)
    assert image.rgba == bytes([
        255,0,0,255,
        0,0,255,255,
        0,0,255,255,
    ])


def test_stencil_decode_zero_paints_one_is_transparent():
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "ImageMask": True,
    }), bytes([0b01000000])))
    image = decode_image(doc, ref)
    assert image.rgba[3] == 255
    assert image.rgba[7] == 0


def test_explicit_mask_uses_mask_alpha_not_white_luminance():
    builder = PDFBuilder(version="1.7")
    mask = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "ImageMask": True,
    }), bytes([0b01000000])))
    image_ref = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceRGB"), "Mask": mask,
    }), bytes([255,0,0, 255,0,0])))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(PDFDict({"Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,10,10]}))
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    image = decode_image(doc, image_ref)
    assert [image.rgba[3], image.rgba[7]] == [255, 0]


def test_soft_mask_uses_grayscale_luminosity():
    builder = PDFBuilder(version="1.7")
    smask = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceGray"),
    }), bytes([0,255])))
    image_ref = builder.add(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 2, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceRGB"), "SMask": smask,
    }), bytes([255,0,0, 255,0,0])))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = builder.add(PDFDict({"Type": PDFName("Page"), "Parent": pages_ref, "MediaBox": [0,0,10,10]}))
    pages["Kids"] = [page]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    image = decode_image(doc, image_ref)
    assert [image.rgba[3], image.rgba[7]] == [0, 255]


def test_dctdecode_calls_owned_jpeg_decoder():
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 8, "Height": 8, "BitsPerComponent": 8,
        "ColorSpace": PDFName("DeviceGray"), "Filter": PDFName("DCTDecode"),
    }), minimal_gray_jpeg()))
    image = decode_image(doc, ref)
    assert image.rgba[:4] == bytes([128,128,128,255])


def test_separation_color_space_uses_owned_tint_function():
    # Separation alternate DeviceRGB, tint t -> [t, 0, 0].
    function = PDFDict({
        "FunctionType": 2, "Domain": [0,1], "Range": [0,1,0,1,0,1],
        "C0": [0,0,0], "C1": [1,0,0], "N": 1,
    })
    doc, ref = _doc(PDFStream(PDFDict({
        "Type": PDFName("XObject"), "Subtype": PDFName("Image"),
        "Width": 1, "Height": 1, "BitsPerComponent": 8,
        "ColorSpace": [PDFName("Separation"), PDFName("Spot"), PDFName("DeviceRGB"), function],
    }), bytes([128])))
    image = decode_image(doc, ref)
    assert 127 <= image.rgba[0] <= 129
    assert image.rgba[1:4] == bytes([0,0,255])
