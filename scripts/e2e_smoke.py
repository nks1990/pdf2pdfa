"""Manual end-to-end smoke test for the external PDF/A toolchain."""

from __future__ import annotations

import base64
from pathlib import Path
import tempfile

import pikepdf

from pdf2pdfa import Converter

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_B64 = ROOT / "tests" / "data" / "sample.pdf.b64"


def main() -> int:
    source_bytes = base64.b64decode(SAMPLE_B64.read_text(encoding="ascii"))

    with tempfile.TemporaryDirectory(prefix="pdf2pdfa-e2e-") as tempdir_name:
        tempdir = Path(tempdir_name)
        source = tempdir / "source.pdf"
        source.write_bytes(source_bytes)

        for level in ("1b", "2b", "3b"):
            output = tempdir / f"output-{level}.pdf"
            converter = Converter(
                level=level,
                backend="ghostscript",
                validate=True,
                fidelity="warn",
            )
            result = converter.convert(source, output)
            if result.validation is None or not result.validation.compliant:
                raise SystemExit(f"PDF/A-{level}: veraPDF validation did not pass")
            if not output.exists() or output.stat().st_size == 0:
                raise SystemExit(f"PDF/A-{level}: no output produced")
            with pikepdf.Pdf.open(output) as pdf:
                if len(pdf.pages) != 1:
                    raise SystemExit(
                        f"PDF/A-{level}: page count changed ({len(pdf.pages)} != 1)"
                    )
            print(
                f"PDF/A-{level}: PASS "
                f"(backend={result.backend}, fidelity="
                f"{'pass' if result.fidelity and result.fidelity.passed else 'warn/off'})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
