from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from pdf2pdfa.backends.base import ConversionBackendError
from pdf2pdfa.backends.ghostscript import GhostscriptBackend


def test_pdfa_definition_has_explicit_component_count(tmp_path):
    profile = tmp_path / "profile.icc"
    profile.write_bytes(b"x" * 128)
    text = GhostscriptBackend._definition(profile, 3, "sRGB")
    assert "/N 3" in text
    assert "/S /GTS_PDFA1" in text
    assert "/OutputConditionIdentifier (sRGB)" in text


def test_backend_discovers_common_executable_names(monkeypatch):
    def fake_which(name):
        return "/usr/bin/gs" if name == "gs" else None

    monkeypatch.setattr("pdf2pdfa.backends.ghostscript.shutil.which", fake_which)
    assert GhostscriptBackend._discover() == "/usr/bin/gs"


def test_source_only_cmyk_profile_is_not_accepted_as_output_target(monkeypatch, tmp_path):
    backend = GhostscriptBackend(executable="gs")
    monkeypatch.setattr(backend, "available", lambda: True)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-placeholder")
    cmyk = Path(str(files("pdf2pdfa").joinpath("data/CMYK.icc.b64")))

    with pytest.raises(ConversionBackendError, match="B2A"):
        backend.convert(source, tmp_path / "out.pdf", level="2b", icc_profile=cmyk)
