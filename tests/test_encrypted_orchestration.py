from __future__ import annotations

from pathlib import Path

import pikepdf

from pdf2pdfa.backends.base import BackendResult
from pdf2pdfa.orchestrator import ConversionOrchestrator


def test_encrypted_input_is_decrypted_before_backend(tmp_path):
    source = tmp_path / "encrypted.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.save(
        source,
        encryption=pikepdf.Encryption(owner="owner", user="top-secret", R=6),
    )
    pdf.close()

    seen: dict[str, object] = {}

    class InspectingBackend:
        name = "pikepdf"

        def available(self):
            return True

        def convert(self, input_path, output_path, **kwargs):
            seen["input_path"] = Path(input_path)
            seen["kwargs"] = dict(kwargs)
            with pikepdf.Pdf.open(input_path) as opened:
                seen["encrypted"] = opened.is_encrypted
            Path(output_path).write_bytes(b"candidate")
            return BackendResult(self.name, Path(output_path))

    orchestrator = ConversionOrchestrator(backend="pikepdf", validate=False)
    orchestrator.fast = InspectingBackend()
    result = orchestrator.convert(
        source,
        tmp_path / "output.pdf",
        level="1b",
        password="top-secret",
    )

    assert seen["encrypted"] is False
    assert Path(seen["input_path"]) != source
    assert "password" not in seen["kwargs"]
    assert result.source_was_encrypted is True
