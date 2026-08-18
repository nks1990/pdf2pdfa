from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pikepdf
import pytest

from pdf2pdfa.colorspace import ColorProfileMismatchError, normalize_resource_color_spaces
from pdf2pdfa.icc import make_icc_stream, read_icc_profile


def test_bundled_profiles_declare_expected_spaces():
    rgb = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/sRGB.icc.b64"))))
    cmyk = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/CMYK.icc.b64"))))
    assert rgb.color_space == "RGB "
    assert rgb.components == 3
    assert cmyk.color_space == "CMYK"
    assert cmyk.components == 4


def test_rgb_role_rejects_cmyk_profile():
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    cmyk = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/CMYK.icc.b64"))))
    stream = make_icc_stream(pdf, cmyk)
    with pytest.raises(ColorProfileMismatchError):
        normalize_resource_color_spaces(pdf, rgb_icc_stream=stream)
