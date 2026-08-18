from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pikepdf
import pytest

from pdf2pdfa.colorspace import ColorProfileMismatchError, normalize_resource_color_spaces
from pdf2pdfa.icc import make_icc_stream, read_icc_profile


def _synthetic_icc(path: Path, color_space: bytes) -> Path:
    data = bytearray(128)
    data[0:4] = (128).to_bytes(4, "big")
    data[12:16] = b"mntr"
    data[16:20] = color_space
    data[20:24] = b"XYZ "
    data[36:40] = b"acsp"
    path.write_bytes(bytes(data))
    return path


def test_bundled_srgb_profile_is_rgb():
    rgb = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/sRGB.icc.b64"))))
    assert rgb.color_space == "RGB "
    assert rgb.components == 3


def test_rgb_role_rejects_four_component_profile(tmp_path):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    cmyk = read_icc_profile(_synthetic_icc(tmp_path / "cmyk.icc", b"CMYK"))
    stream = make_icc_stream(pdf, cmyk)
    with pytest.raises(ColorProfileMismatchError):
        normalize_resource_color_spaces(pdf, rgb_icc_stream=stream)
