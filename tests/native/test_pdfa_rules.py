from __future__ import annotations

from pathlib import Path

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.color_profiles import srgb_profile_bytes
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.pdfa import NativePDFAValidator
from pdf2pdfa.native.xmp import build_pdfa_xmp


def _candidate(
    *,
    level: str = "1b",
    content: bytes = b"q Q\n",
    resources: PDFDict | None = None,
    catalog_extra: PDFDict | None = None,
    extra_objects: list[tuple[str, object]] | None = None,
) -> bytes:
    builder = PDFBuilder(version="1.4" if level == "1b" else "1.7")

    icc = PDFStream(PDFDict({"N": 3}), srgb_profile_bytes())
    icc_ref = builder.add(icc)
    metadata_ref = builder.add(
        PDFStream(
            PDFDict({"Type": PDFName("Metadata"), "Subtype": PDFName("XML")}),
            build_pdfa_xmp(part=int(level[0]), conformance="B"),
        )
    )
    contents_ref = builder.add(PDFStream(PDFDict(), content))

    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page = PDFDict(
        {
            "Type": PDFName("Page"),
            "Parent": pages_ref,
            "MediaBox": [0, 0, 200, 200],
            "Resources": resources or PDFDict(),
            "Contents": contents_ref,
        }
    )
    page_ref = builder.add(page)
    pages["Kids"] = [page_ref]

    output_intent = PDFDict(
        {
            "Type": PDFName("OutputIntent"),
            "S": PDFName("GTS_PDFA1"),
            "OutputConditionIdentifier": b"pdf2pdfa sRGB",
            "DestOutputProfile": icc_ref,
        }
    )
    catalog = PDFDict(
        {
            "Type": PDFName("Catalog"),
            "Pages": pages_ref,
            "Metadata": metadata_ref,
            "OutputIntents": [output_intent],
        }
    )
    if catalog_extra:
        catalog.update(catalog_extra)
    root_ref = builder.add(catalog)
    builder.set_root(root_ref)
    return builder.to_bytes()


def test_minimal_owned_pdfa1b_passes():
    report = NativePDFAValidator().validate(_candidate(level="1b"), "1b")
    assert report.compliant, report.failures
    assert report.engine == "pdf2pdfa-owned"


def test_minimal_owned_pdfa2b_passes():
    report = NativePDFAValidator().validate(_candidate(level="2b"), "2b")
    assert report.compliant, report.failures


def test_minimal_owned_pdfa3b_passes():
    report = NativePDFAValidator().validate(_candidate(level="3b"), "3b")
    assert report.compliant, report.failures


def test_missing_metadata_fails_closed():
    pdf = _candidate(level="1b")
    # Build a second deliberately plain file because byte surgery would make
    # xref offsets invalid and would test repair mode instead of conformance.
    builder = PDFBuilder(version="1.4")
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 10, 10],
                "Resources": PDFDict(),
            }
        )
    )
    pages["Kids"] = [page_ref]
    root_ref = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root_ref)
    report = NativePDFAValidator().validate(builder.to_bytes(), "1b")
    assert not report.compliant
    assert "metadata.required" in report.failed_rules


def test_javascript_action_is_rejected():
    pdf = _candidate(
        level="2b",
        catalog_extra=PDFDict(
            {"OpenAction": PDFDict({"S": PDFName("JavaScript"), "JS": b"app.alert('x')"})}
        ),
    )
    report = NativePDFAValidator().validate(pdf, "2b")
    assert not report.compliant
    assert "actions.forbidden" in report.failed_rules


def test_pdfa1_transparency_is_rejected_but_pdfa2_accepts_it():
    resources = PDFDict(
        {
            "ExtGState": PDFDict(
                {"GS0": PDFDict({"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5})}
            )
        }
    )
    a1 = NativePDFAValidator().validate(
        _candidate(level="1b", content=b"/GS0 gs\n", resources=resources), "1b"
    )
    a2 = NativePDFAValidator().validate(
        _candidate(level="2b", content=b"/GS0 gs\n", resources=resources), "2b"
    )
    assert not a1.compliant
    assert "pdfa1.transparency" in a1.failed_rules
    assert a2.compliant, a2.failures


def test_device_rgb_is_characterized_by_rgb_output_intent():
    report = NativePDFAValidator().validate(
        _candidate(level="1b", content=b"1 0 0 rg\n"), "1b"
    )
    assert report.compliant, report.failures


def test_device_cmyk_requires_matching_characterization():
    report = NativePDFAValidator().validate(
        _candidate(level="1b", content=b"0 0 0 1 k\n"), "1b"
    )
    assert not report.compliant
    assert "color.unmanaged_device_space" in report.failed_rules


def _attachment_candidate(level: str, *, mime: str, relationship: str | None = None) -> bytes:
    builder = PDFBuilder(version="1.4" if level == "1b" else "1.7")
    icc_ref = builder.add(PDFStream(PDFDict({"N": 3}), srgb_profile_bytes()))
    metadata_ref = builder.add(
        PDFStream(
            PDFDict({"Type": PDFName("Metadata"), "Subtype": PDFName("XML")}),
            build_pdfa_xmp(part=int(level[0]), conformance="B"),
        )
    )
    attachment = PDFStream(
        PDFDict({"Type": PDFName("EmbeddedFile"), "Subtype": PDFName(mime)}),
        b"archival attachment\n",
    )
    attachment_ref = builder.add(attachment)
    filespec = PDFDict(
        {
            "Type": PDFName("Filespec"),
            "F": b"note.bin",
            "UF": b"note.bin",
            "EF": PDFDict({"F": attachment_ref}),
        }
    )
    if relationship:
        filespec["AFRelationship"] = PDFName(relationship)
    filespec_ref = builder.add(filespec)

    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 10, 10],
                "Resources": PDFDict(),
            }
        )
    )
    pages["Kids"] = [page_ref]
    catalog = PDFDict(
        {
            "Type": PDFName("Catalog"),
            "Pages": pages_ref,
            "Metadata": metadata_ref,
            "OutputIntents": [
                PDFDict(
                    {
                        "Type": PDFName("OutputIntent"),
                        "S": PDFName("GTS_PDFA1"),
                        "DestOutputProfile": icc_ref,
                    }
                )
            ],
            "Names": PDFDict(
                {
                    "EmbeddedFiles": PDFDict(
                        {"Names": [b"note.bin", filespec_ref]}
                    )
                }
            ),
        }
    )
    if level == "3b":
        catalog["AF"] = [filespec_ref]
    root_ref = builder.add(catalog)
    builder.set_root(root_ref)
    return builder.to_bytes()


def test_pdfa1_rejects_attachments():
    report = NativePDFAValidator().validate(
        _attachment_candidate("1b", mime="text/plain"), "1b"
    )
    assert not report.compliant
    assert "pdfa1.embedded_files" in report.failed_rules


def test_pdfa2_accepts_plain_text_attachment():
    report = NativePDFAValidator().validate(
        _attachment_candidate("2b", mime="text/plain"), "2b"
    )
    assert report.compliant, report.failures


def test_pdfa2_rejects_arbitrary_binary_attachment():
    report = NativePDFAValidator().validate(
        _attachment_candidate("2b", mime="application/octet-stream"), "2b"
    )
    assert not report.compliant
    assert "pdfa2.embedded_file_type" in report.failed_rules


def test_pdfa3_requires_af_relationship():
    report = NativePDFAValidator().validate(
        _attachment_candidate("3b", mime="application/octet-stream"), "3b"
    )
    assert not report.compliant
    assert "pdfa3.af_relationship" in report.failed_rules


def test_pdfa3_accepts_arbitrary_attachment_with_relationship():
    report = NativePDFAValidator().validate(
        _attachment_candidate(
            "3b", mime="application/octet-stream", relationship="Data"
        ),
        "3b",
    )
    assert report.compliant, report.failures
