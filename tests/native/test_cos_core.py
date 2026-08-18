from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native import PDFDocument, PDFName, PDFRef, PDFStream, PDFWriter
from pdf2pdfa.native.objects import PDFDict
from pdf2pdfa.native.tokenizer import PDFTokenizer


def _classic_pdf() -> bytes:
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    bodies = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources <<>> >>",
        4: b"<< /Length 5 0 R >>\nstream\nhello\nendstream",
        5: b"5",
    }
    output = bytearray(header)
    offsets = {0: 0}
    for number in sorted(bodies):
        offsets[number] = len(output)
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(bodies[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for number in range(1, 6):
        output.extend(f"{offsets[number]:010d} 00000 n \n".encode())
    output.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n")
    output.extend(str(xref).encode())
    output.extend(b"\n%%EOF\n")
    return bytes(output)


def _xref_stream_pdf() -> bytes:
    output = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def add(number: int, body: bytes) -> None:
        offsets[number] = len(output)
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, b"<< /Type /Pages /Count 0 /Kids [] >>")
    objstm_data = b"4 0 << /Producer (native) >>"
    add(3, f"<< /Type /ObjStm /N 1 /First 4 /Length {len(objstm_data)} >>\nstream\n".encode() + objstm_data + b"\nendstream")

    offsets[5] = len(output)
    rows = bytearray()
    entries = {
        0: (0, 0, 65535),
        1: (1, offsets[1], 0),
        2: (1, offsets[2], 0),
        3: (1, offsets[3], 0),
        4: (2, 3, 0),
        5: (1, offsets[5], 0),
    }
    for number in range(6):
        kind, field2, field3 = entries[number]
        rows.extend(kind.to_bytes(1, "big"))
        rows.extend(field2.to_bytes(4, "big"))
        rows.extend(field3.to_bytes(2, "big"))
    xref_body = (
        f"<< /Type /XRef /Size 6 /Root 1 0 R /Info 4 0 R /W [1 4 2] /Length {len(rows)} >>\nstream\n".encode()
        + bytes(rows)
        + b"\nendstream"
    )
    add(5, xref_body)
    output.extend(b"startxref\n")
    output.extend(str(offsets[5]).encode())
    output.extend(b"\n%%EOF\n")
    return bytes(output)


def test_tokenizer_preserves_names_strings_and_refs():
    tokenizer = PDFTokenizer(b"<< /A#20B (x\\n\\050y\\051) /Ref 12 3 R /Hex <4142F> >>")
    obj = tokenizer.parse_object()
    assert isinstance(obj, PDFDict)
    assert obj["A B"] == b"x\n(y)"
    assert obj["Ref"] == PDFRef(12, 3)
    assert obj["Hex"] == b"AB\xf0"


def test_parse_classic_xref_and_indirect_stream_length(tmp_path: Path):
    path = tmp_path / "classic.pdf"
    path.write_bytes(_classic_pdf())
    doc = PDFDocument.open(path, repair=False)
    assert doc.header_version == "1.4"
    assert doc.catalog["Type"] == PDFName("Catalog")
    stream = doc.get(PDFRef(4, 0))
    assert isinstance(stream, PDFStream)
    assert stream.data == b"hello"


def test_parse_xref_and_object_stream():
    doc = PDFDocument.open(_xref_stream_pdf(), repair=False)
    info = doc.get(PDFRef(4, 0))
    assert isinstance(info, PDFDict)
    assert info["Producer"] == b"native"
    assert doc.catalog["Type"] == PDFName("Catalog")


def test_writer_materializes_object_stream_members_and_roundtrips(tmp_path: Path):
    source = PDFDocument.open(_xref_stream_pdf(), repair=False)
    output = tmp_path / "roundtrip.pdf"
    PDFWriter(source, version="1.4").write(output)
    data = output.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert b"/ObjStm" not in data
    assert b"/XRef" not in data
    assert b"4 0 obj" in data

    parsed = PDFDocument.open(data, repair=False)
    assert parsed.catalog["Type"] == PDFName("Catalog")
    info = parsed.get(PDFRef(4, 0))
    assert isinstance(info, PDFDict)
    assert info["Producer"] == b"native"
