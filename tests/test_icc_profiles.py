from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pikepdf
import pytest

from pdf2pdfa.colorspace import ColorProfileMismatchError, normalize_resource_color_spaces
from pdf2pdfa.icc import InvalidICCProfileError, make_icc_stream, read_icc_profile


def test_bundled_profiles_declare_expected_spaces_and_mappings():
    rgb = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/sRGB.icc.b64"))))
    cmyk = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/CMYK.icc.b64"))))

    assert rgb.color_space == "RGB "
    assert rgb.components == 3
    assert {"rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC"}.issubset(rgb.tags)

    assert cmyk.color_space == "CMYK"
    assert cmyk.components == 4
    assert "A2B0" in cmyk.tags
    # Guard against accidentally replacing the real LUT profile with a header-only stub.
    assert len(cmyk.data) > 8000


def test_cmyk_header_without_device_mapping_is_rejected(tmp_path):
    data = bytearray(132)
    data[0:4] = (132).to_bytes(4, "big")
    data[12:16] = b"scnr"
    data[16:20] = b"CMYK"
    data[20:24] = b"Lab "
    data[36:40] = b"acsp"
    data[128:132] = (0).to_bytes(4, "big")
    path = tmp_path / "stub.icc"
    path.write_bytes(data)

    with pytest.raises(InvalidICCProfileError, match="A2B"):
        read_icc_profile(path)


def test_rgb_role_rejects_cmyk_profile():
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    cmyk = read_icc_profile(Path(str(files("pdf2pdfa").joinpath("data/CMYK.icc.b64"))))
    stream = make_icc_stream(pdf, cmyk)
    with pytest.raises(ColorProfileMismatchError):
        normalize_resource_color_spaces(pdf, rgb_icc_stream=stream)
