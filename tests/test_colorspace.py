"""Tests for conservative color-space normalization."""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf
import pytest

from pdf2pdfa.colorspace import (
    FullColorRewriteRequired,
    normalize_resource_color_spaces,
    sanitize_color_spaces,
)


def _image(pdf: Pdf, color_space: str, sample: bytes):
    image = pdf.make_stream(sample)
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/BitsPerComponent"] = 8
    image["/ColorSpace"] = Name(color_space)
    return image


def test_device_rgb_replaced_in_xobject():
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Resources["/XObject"] = Dictionary(
        {"/Im0": _image(pdf, "/DeviceRGB", bytes([255, 0, 0]))}
    )
    rgb_icc = pdf.make_stream(b"\x00" * 128)
    rgb_icc["/N"] = 3

    sanitize_color_spaces(pdf, rgb_icc)

    color_space = page.Resources["/XObject"]["/Im0"]["/ColorSpace"]
    assert isinstance(color_space, Array)
    assert color_space[0] == Name("/ICCBased")
    assert int(color_space[1]["/N"]) == 3


def test_device_cmyk_requires_full_rewrite_without_source_profile():
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Resources["/XObject"] = Dictionary(
        {"/Im0": _image(pdf, "/DeviceCMYK", bytes([0, 0, 0, 255]))}
    )

    with pytest.raises(FullColorRewriteRequired, match="DeviceCMYK"):
        normalize_resource_color_spaces(pdf)


def test_explicit_cmyk_profile_can_be_assigned_when_intentional():
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Resources["/XObject"] = Dictionary(
        {"/Im0": _image(pdf, "/DeviceCMYK", bytes([0, 0, 0, 255]))}
    )
    cmyk_icc = pdf.make_stream(b"fixture-profile")
    cmyk_icc["/N"] = 4

    normalize_resource_color_spaces(pdf, cmyk_icc_stream=cmyk_icc)

    color_space = page.Resources["/XObject"]["/Im0"]["/ColorSpace"]
    assert isinstance(color_space, Array)
    assert color_space[0] == Name("/ICCBased")
    assert int(color_space[1]["/N"]) == 4
