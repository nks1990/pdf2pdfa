"""Local/tag release sanity checks with no network dependency."""

from __future__ import annotations

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

    expected = [
        ROOT / "README.md",
        ROOT / "LICENSE",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "CHANGELOG.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "COMPLIANCE.md",
        ROOT / "docs" / "RENDERER_SUPPORT.md",
        ROOT / "docs" / "TESTING.md",
        ROOT / "pdf2pdfa" / "native" / "document.py",
        ROOT / "pdf2pdfa" / "native" / "pdfa.py",
        ROOT / "pdf2pdfa" / "native" / "pipeline.py",
        ROOT / "pdf2pdfa" / "native" / "render.py",
        ROOT / "pdf2pdfa" / "native" / "predefined_cmap_data.py",
        ROOT / "pdf2pdfa" / "py.typed",
        ROOT / "tests" / "native" / "test_native_module_graph.py",
        ROOT / "tests" / "owned" / "test_package_ownership.py",
    ]
    missing = [str(path.relative_to(ROOT)) for path in expected if not path.exists()]
    if missing:
        fail("missing release files: " + ", ".join(missing))

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

    tag = os.environ.get("GITHUB_REF_NAME") or os.environ.get("PDF2PDFA_RELEASE_TAG")
    if tag:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            fail(f"tag {tag!r} does not match package version {expected_tag!r}")

    print(f"release-check: pdf2pdfa {version} owned-runtime OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
