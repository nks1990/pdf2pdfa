"""Local/tag release sanity checks with no network dependency."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"release-check: {message}")


def _version(pyproject: str) -> str:
    project_match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", pyproject)
    if project_match is None:
        fail("pyproject.toml has no [project] table")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project_match.group(1))
    if match is None:
        fail("pyproject.toml has no literal project version")
    return match.group(1).strip()


def _check_action_pins(workflow_text: str) -> None:
    for raw_line in workflow_text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
            continue
        target = stripped.split("uses:", 1)[1].strip().split("#", 1)[0].strip()
        if target.startswith("./"):
            continue
        if "@" not in target:
            fail(f"workflow action is missing a ref: {target}")
        _action, revision = target.rsplit("@", 1)
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            fail(f"workflow action must be pinned to a 40-character commit SHA: {target}")


def main() -> int:
    pyproject_path = ROOT / "pyproject.toml"
    text = pyproject_path.read_text(encoding="utf-8")
    version = _version(text)
    if not version or version.startswith("0+"):
        fail(f"invalid project version: {version!r}")

    dependencies = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", text, re.S)
    if dependencies is None or dependencies.group(1).strip():
        fail("runtime dependencies must be explicitly empty")

    if 'license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]' not in text:
        fail("license-files must include LICENSE and THIRD_PARTY_NOTICES.md")

    manifest_path = ROOT / "MANIFEST.in"
    manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    manifest_lines = manifest.splitlines()
    if "include THIRD_PARTY_NOTICES.md" not in manifest_lines:
        fail("MANIFEST.in must include THIRD_PARTY_NOTICES.md")
    schema_packaged = (
        "include docs/agent-protocol-v1.schema.json" in manifest_lines
        or any(
            line.startswith("recursive-include docs") and "*.json" in line.split()
            for line in manifest_lines
        )
    )
    if not schema_packaged:
        fail("MANIFEST.in must include the agent protocol JSON schema")

    expected = [
        ROOT / "README.md",
        ROOT / "LICENSE",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "CHANGELOG.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "MANIFEST.in",
        ROOT / "docs" / "AGENT_INTEGRATION.md",
        ROOT / "docs" / "agent-protocol-v1.schema.json",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "COMPLIANCE.md",
        ROOT / "docs" / "RENDERER_SUPPORT.md",
        ROOT / "docs" / "TESTING.md",
        ROOT / "pdf2pdfa" / "agent_protocol.py",
        ROOT / "pdf2pdfa" / "native" / "document.py",
        ROOT / "pdf2pdfa" / "native" / "pdfa.py",
        ROOT / "pdf2pdfa" / "native" / "pipeline.py",
        ROOT / "pdf2pdfa" / "native" / "render.py",
        ROOT / "pdf2pdfa" / "native" / "predefined_cmap_data.py",
        ROOT / "pdf2pdfa" / "py.typed",
        ROOT / "scripts" / "check.py",
        ROOT / "scripts" / "corpus_check.py",
        ROOT / "scripts" / "external_oracle_check.py",
        ROOT / "scripts" / "e2e_smoke.py",
        ROOT / "scripts" / "wheel_smoke.py",
        ROOT / "tests" / "native" / "test_native_module_graph.py",
        ROOT / "tests" / "owned" / "test_agent_cli.py",
        ROOT / "tests" / "owned" / "test_agent_protocol_schema.py",
        ROOT / "tests" / "owned" / "test_package_ownership.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in expected if not path.exists()]
    if missing:
        fail("missing release files: " + ", ".join(missing))

    protocol_text = (ROOT / "pdf2pdfa" / "agent_protocol.py").read_text(encoding="utf-8")
    if 'MACHINE_SCHEMA_VERSION = "1"' not in protocol_text:
        fail("agent machine protocol schema v1 contract is missing")

    schema_path = ROOT / "docs" / "agent-protocol-v1.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"agent protocol JSON schema is invalid: {exc}")
    if schema.get("properties", {}).get("schema_version", {}).get("const") != "1":
        fail("agent protocol JSON schema must describe schema_version 1")

    workflow_dir = ROOT / ".github" / "workflows"
    workflows = (
        sorted(path.name for path in workflow_dir.glob("*.y*ml") if path.is_file())
        if workflow_dir.exists()
        else []
    )
    unexpected = [name for name in workflows if name != "release.yml"]
    if unexpected:
        fail("continuous/inadvertent workflows are present: " + ", ".join(unexpected))
    if workflows != ["release.yml"]:
        fail("release.yml must be the only GitHub Actions workflow")

    workflow_text = (workflow_dir / "release.yml").read_text(encoding="utf-8")
    if 'tags:\n      - "v*.*.*"' not in workflow_text:
        fail("release workflow must be tag-triggered")
    if "python scripts/check.py --full" not in workflow_text:
        fail("release workflow must run the full owned gate before publication")
    if "pypa/gh-action-pypi-publish" not in workflow_text:
        fail("release workflow is missing PyPI publication action")
    if "persist-credentials: false" not in workflow_text:
        fail("release checkout must not persist Git credentials")
    if "git merge-base --is-ancestor" not in workflow_text:
        fail("release workflow must verify the tagged commit is reachable from main")
    _check_action_pins(workflow_text)

    tag = os.environ.get("GITHUB_REF_NAME") or os.environ.get("PDF2PDFA_RELEASE_TAG")
    if tag:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            fail(f"tag {tag!r} does not match package version {expected_tag!r}")

    print(f"release-check: pdf2pdfa {version} owned-runtime OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
