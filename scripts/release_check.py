"""Local/tag release sanity checks with no network dependency."""

from __future__ import annotations

import os
from pathlib import Path
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"release-check: {message}")


def main() -> int:
    pyproject_path = ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]
    version = str(project["version"]).strip()
    if not version or version.startswith("0+"):
        fail(f"invalid project version: {version!r}")

    expected = [
        ROOT / "README.md",
        ROOT / "LICENSE",
        ROOT / "CHANGELOG.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "pdf2pdfa" / "data" / "sRGB.icc.b64",
        ROOT / "pdf2pdfa" / "data" / "CMYK.icc.b64",
        ROOT / "pdf2pdfa" / "py.typed",
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

    tag = os.environ.get("GITHUB_REF_NAME") or os.environ.get("PDF2PDFA_RELEASE_TAG")
    if tag:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            fail(f"tag {tag!r} does not match package version {expected_tag!r}")

    print(f"release-check: pdf2pdfa {version} OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
