"""Tests for color space sanitization."""
from pathlib import Path

import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array, Stream
from pdf2pdfa.colorspace import sanitize_color_spaces

DATA_DIR = Path(__file__).parent / 'data'


def test_device_rgb_replaced_in_xobject(tmp_path):
    """Verify DeviceRGB on image XObjects is replaced with ICCBased."""
    # Create a minimal PDF with a DeviceRGB image XObject
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    # Create a tiny 1x1 RGB image
    img_data = bytes([255, 0, 0])  # red pixel
    img_stream = pdf.make_stream(img_data)
    img_stream.stream_dict['/Type'] = Name('/XObject')
    img_stream.stream_dict['/Subtype'] = Name('/Image')
    img_stream.stream_dict['/Width'] = 1
    img_stream.stream_dict['/Height'] = 1
    img_stream.stream_dict['/BitsPerComponent'] = 8
    img_stream.stream_dict['/ColorSpace'] = Name('/DeviceRGB')

    if '/Resources' not in page:
        page['/Resources'] = Dictionary()
    page.Resources['/XObject'] = Dictionary({'/Im0': img_stream})

    # Create a fake RGB ICC stream
    rgb_icc = pdf.make_stream(b'\x00' * 128)
    rgb_icc.stream_dict['/N'] = 3

    sanitize_color_spaces(pdf, rgb_icc)

    # Check that DeviceRGB was replaced
    cs = page.Resources['/XObject']['/Im0']['/ColorSpace']
    assert isinstance(cs, Array)
    assert cs[0] == Name('/ICCBased')


def test_device_cmyk_replaced_in_xobject(tmp_path):
    """Verify DeviceCMYK on image XObjects is replaced with ICCBased."""
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    img_data = bytes([0, 0, 0, 255])  # CMYK pixel
    img_stream = pdf.make_stream(img_data)
    img_stream.stream_dict['/Type'] = Name('/XObject')
    img_stream.stream_dict['/Subtype'] = Name('/Image')
    img_stream.stream_dict['/Width'] = 1
    img_stream.stream_dict['/Height'] = 1
    img_stream.stream_dict['/BitsPerComponent'] = 8
    img_stream.stream_dict['/ColorSpace'] = Name('/DeviceCMYK')

    if '/Resources' not in page:
        page['/Resources'] = Dictionary()
    page.Resources['/XObject'] = Dictionary({'/Im0': img_stream})

    rgb_icc = pdf.make_stream(b'\x00' * 128)
    rgb_icc.stream_dict['/N'] = 3

    sanitize_color_spaces(pdf, rgb_icc)

    cs = page.Resources['/XObject']['/Im0']['/ColorSpace']
    assert isinstance(cs, Array)
    assert cs[0] == Name('/ICCBased')
    # CMYK stream should have N=4
    icc_stream = cs[1]
    assert int(icc_stream.stream_dict['/N']) == 4
