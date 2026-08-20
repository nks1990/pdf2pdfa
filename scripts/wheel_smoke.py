"""Install a built wheel into an isolated venv and exercise the shipped package.

This catches packaging mistakes that checkout/editable-install tests cannot see.
The venv installs the local wheel with ``--no-deps``; pdf2pdfa v5 has no runtime
dependencies. The owned E2E script is then executed with the venv interpreter,
so imports resolve from the installed wheel rather than the repository package.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def _venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="built pdf2pdfa wheel")
    args = parser.parse_args()

    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"wheel-smoke: wheel not found: {wheel}")

    with tempfile.TemporaryDirectory(prefix="pdf2pdfa-wheel-") as tempdir_name:
        environment = Path(tempdir_name) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        if not python.is_file():
            raise SystemExit(f"wheel-smoke: venv interpreter missing: {python}")

        _run(
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        )
        _run(str(python), "-m", "pdf2pdfa", "--version")

        # Run from the scripts directory so the repository root is not placed
        # on sys.path ahead of site-packages. This exercises the installed
        # wheel while reusing the canonical generated-PDF E2E smoke driver.
        _run(
            str(python),
            str(ROOT / "scripts" / "e2e_smoke.py"),
            cwd=ROOT / "scripts",
        )

    print("wheel-smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
