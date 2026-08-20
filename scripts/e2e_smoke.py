"""Manual end-to-end smoke test for the fully owned PDF/A stack."""

from __future__ import annotations

from pathlib import Path
import tempfile

from pdf2pdfa import Converter
from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream
from pdf2pdfa.native.structure import walk_pages


def sample(*, transparent: bool = False) -> bytes:
    builder = PDFBuilder(version="1.7")
    resources = PDFDict()
    if transparent:
        resources["ExtGState"] = PDFDict(
            {
                "GS": PDFDict(
                    {"Type": PDFName("ExtGState"), "ca": 0.5, "CA": 0.5}
                )
            }
        )
        content = b"/GS gs\n1 0 0 rg\n20 20 100 80 re f\n"
    else:
        content = b"0 0 0 rg\n20 20 100 80 re f\n"
    content_ref = builder.add(PDFStream(PDFDict(), content))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    page_ref = builder.add(
        PDFDict(
            {
                "Type": PDFName("Page"),
                "Parent": pages_ref,
                "MediaBox": [0, 0, 200, 150],
                "Resources": resources,
                "Contents": content_ref,
            }
        )
    )
    pages["Kids"] = [page_ref]
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    return builder.to_bytes()


def assert_owned_output(path: Path, level: str) -> None:
    report = Converter(level=level).validate(path)
    if not report.compliant:
        details = "; ".join(
            f"{item.rule_id}:{item.path}" for item in report.failures[:10]
        )
        raise SystemExit(f"PDF/A-{level}: owned validation failed: {details}")
    doc = PDFDocument.open(path, repair=False)
    pages = list(walk_pages(doc))
    if len(pages) != 1:
        raise SystemExit(f"PDF/A-{level}: page count changed ({len(pages)} != 1)")
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"PDF/A-{level}: no output produced")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pdf2pdfa-e2e-") as tempdir_name:
        tempdir = Path(tempdir_name)
        plain = tempdir / "plain.pdf"
        transparent = tempdir / "transparent.pdf"
        plain.write_bytes(sample())
        transparent.write_bytes(sample(transparent=True))

        for level in ("1b", "2b", "3b"):
            source = transparent if level == "1b" else plain
            output = tempdir / f"output-{level}.pdf"
            result = Converter(
                level=level,
                fidelity="auto",
                transparency_dpi=72,
                visual_dpi=72,
            ).convert(source, output)
            if not result.validation.compliant:
                raise SystemExit(f"PDF/A-{level}: conversion result was not compliant")
            assert_owned_output(output, level)
            print(
                f"PDF/A-{level}: PASS "
                f"(engine={result.engine}, fidelity={result.fidelity_mode})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
