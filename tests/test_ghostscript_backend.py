from __future__ import annotations

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
