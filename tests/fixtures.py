"""Small, generated PDF fixtures for structural regression tests.

The suite intentionally generates pathological features instead of checking in
large opaque binary documents. Each fixture isolates one PDF/A-relevant feature
so failures are easy to diagnose.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def blank_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(path)
    pdf.close()
    return path


def transparency_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Resources["/ExtGState"] = Dictionary(
        {"/GS0": Dictionary({"/Type": Name("/ExtGState"), "/ca": 0.5, "/CA": 0.5})}
    )
    pdf.save(path)
    pdf.close()
    return path


def javascript_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.Root["/OpenAction"] = Dictionary(
        {"/S": Name("/JavaScript"), "/JS": String("app.alert('test')")}
    )
    pdf.save(path)
    pdf.close()
    return path


def attachment_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    embedded = pdf.make_stream(b"fixture attachment")
    embedded["/Type"] = Name("/EmbeddedFile")
    filespec = Dictionary(
        {
            "/Type": Name("/Filespec"),
            "/F": String("fixture.txt"),
            "/UF": String("fixture.txt"),
            "/EF": Dictionary({"/F": embedded}),
        }
    )
    pdf.Root["/Names"] = Dictionary(
        {"/EmbeddedFiles": Dictionary({"/Names": Array([String("fixture.txt"), filespec])})}
    )
    pdf.save(path)
    pdf.close()
    return path


def signature_pdf(path: Path, *, signed: bool = True) -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    field = Dictionary(
        {
            "/FT": Name("/Sig"),
            "/T": String("Signature1"),
        }
    )
    if signed:
        field["/V"] = Dictionary(
            {
                "/Type": Name("/Sig"),
                "/Filter": Name("/Adobe.PPKLite"),
                "/SubFilter": Name("/adbe.pkcs7.detached"),
                "/ByteRange": Array([0, 0, 0, 0]),
                "/Contents": String("placeholder"),
            }
        )
    pdf.Root["/AcroForm"] = Dictionary({"/Fields": Array([field])})
    pdf.save(path)
    pdf.close()
    return path


def type0_font_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    descriptor = Dictionary(
        {
            "/Type": Name("/FontDescriptor"),
            "/FontName": Name("/FixtureCID"),
            "/Flags": 4,
            "/FontBBox": Array([0, -200, 1000, 900]),
            "/ItalicAngle": 0,
            "/Ascent": 800,
            "/Descent": -200,
            "/CapHeight": 700,
            "/StemV": 80,
        }
    )
    descendant = Dictionary(
        {
            "/Type": Name("/Font"),
            "/Subtype": Name("/CIDFontType2"),
            "/BaseFont": Name("/FixtureCID"),
            "/CIDSystemInfo": Dictionary(
                {
                    "/Registry": String("Adobe"),
                    "/Ordering": String("Identity"),
                    "/Supplement": 0,
                }
            ),
            "/FontDescriptor": descriptor,
        }
    )
    type0 = Dictionary(
        {
            "/Type": Name("/Font"),
            "/Subtype": Name("/Type0"),
            "/BaseFont": Name("/FixtureCID"),
            "/Encoding": Name("/Identity-H"),
            "/DescendantFonts": Array([descendant]),
        }
    )
    page.Resources["/Font"] = Dictionary({"/F0": type0})
    pdf.save(path)
    pdf.close()
    return path


def device_rgb_image_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    image = pdf.make_stream(bytes([255, 0, 0]))
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/BitsPerComponent"] = 8
    image["/ColorSpace"] = Name("/DeviceRGB")
    page.Resources["/XObject"] = Dictionary({"/Im0": image})
    pdf.save(path)
    pdf.close()
    return path
