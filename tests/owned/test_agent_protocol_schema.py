from __future__ import annotations

import json
from pathlib import Path

from pdf2pdfa import MACHINE_SCHEMA_VERSION, __version__
from pdf2pdfa.agent_protocol import envelope, error_payload
from pdf2pdfa.native.pipeline import InputLimitError, SignatureInvalidationError
from pdf2pdfa.native.repair import UnsupportedNativeRepairError
from pdf2pdfa.native.security import InvalidPasswordError


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs" / "agent-protocol-v1.schema.json"


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_schema_version_matches_public_protocol_constant():
    schema = _schema()
    assert MACHINE_SCHEMA_VERSION == "1"
    assert schema["properties"]["schema_version"]["const"] == MACHINE_SCHEMA_VERSION


def test_machine_envelope_includes_package_and_protocol_versions():
    payload = envelope(
        "inspect",
        ok=True,
        status="repairable",
        exit_code=0,
        result={"repairable": True},
    )
    assert payload["schema_version"] == MACHINE_SCHEMA_VERSION
    assert payload["pdf2pdfa_version"] == __version__
    assert payload["command"] == "inspect"
    assert payload["ok"] is True
    assert payload["status"] == "repairable"
    assert payload["exit_code"] == 0


def test_emitted_stable_error_codes_are_declared_by_schema():
    schema = _schema()
    declared = set(schema["$defs"]["error"]["properties"]["code"]["enum"])
    examples = [
        FileNotFoundError("missing"),
        InputLimitError("too large"),
        InvalidPasswordError("wrong password"),
        SignatureInvalidationError("signed"),
        UnsupportedNativeRepairError("unsupported"),
        ValueError("bad argument"),
        OSError("io"),
        RuntimeError("unexpected"),
    ]
    for exc in examples:
        assert error_payload(exc)["code"] in declared


def test_missing_convert_input_is_normalized_to_same_code_as_validate():
    from pdf2pdfa.native.pipeline import OwnedPipelineError

    payload = error_payload(
        OwnedPipelineError("input is not a regular file: /tmp/missing.pdf")
    )
    assert payload["code"] == "INPUT_NOT_FOUND"
    assert payload["category"] == "invalid_input"
