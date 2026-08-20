"""Install a built wheel into an isolated venv and exercise the shipped package.

This catches packaging mistakes that checkout/editable-install tests cannot see.
The venv installs the local wheel with ``--no-deps``; pdf2pdfa v5 has no runtime
dependencies. The owned E2E script is then executed with the venv interpreter,
so imports resolve from the installed wheel rather than the repository package.
"""

from __future__ import annotations

import argparse
from email.parser import Parser
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def _venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _inspect_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(
                f"wheel-smoke: expected one METADATA file, found {len(metadata_names)}"
            )
        metadata = Parser().parsestr(
            archive.read(metadata_names[0]).decode("utf-8", errors="strict")
        )
        if metadata.get("Name", "").lower() != "pdf2pdfa":
            raise SystemExit(f"wheel-smoke: unexpected package name {metadata.get('Name')!r}")
        version = metadata.get("Version", "").strip()
        if not version or version.startswith("0+"):
            raise SystemExit(f"wheel-smoke: invalid wheel version {version!r}")

        requirements = metadata.get_all("Requires-Dist", []) or []
        unconditional = [item for item in requirements if "extra ==" not in item]
        if unconditional:
            raise SystemExit(
                "wheel-smoke: unexpected runtime Requires-Dist: "
                + "; ".join(unconditional)
            )

        license_basenames = {
            Path(name).name
            for name in names
            if ".dist-info/licenses/" in name.replace("\\", "/")
        }
        required_licenses = {"LICENSE", "THIRD_PARTY_NOTICES.md"}
        missing = sorted(required_licenses - license_basenames)
        if missing:
            raise SystemExit(
                "wheel-smoke: missing wheel license/notices: " + ", ".join(missing)
            )

        for required in ("pdf2pdfa/py.typed", "pdf2pdfa/agent_protocol.py"):
            if required not in names:
                raise SystemExit(f"wheel-smoke: {required} is missing from wheel")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="built pdf2pdfa wheel")
    args = parser.parse_args()

    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"wheel-smoke: wheel not found: {wheel}")

    _inspect_wheel(wheel)

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

        import_scan = (
            "import importlib, pkgutil, pdf2pdfa; "
            "mods=[pdf2pdfa.__name__]+[m.name for m in pkgutil.walk_packages("
            "pdf2pdfa.__path__, pdf2pdfa.__name__+'.')]; "
            "[importlib.import_module(name) for name in sorted(set(mods))]; "
            "print('installed-module-scan:', len(set(mods)), 'modules OK')"
        )
        _run(str(python), "-c", import_scan, cwd=ROOT / "scripts")

        protocol_scan = (
            "import pdf2pdfa; "
            "from pdf2pdfa.agent_protocol import MACHINE_SCHEMA_VERSION, envelope, error_payload; "
            "assert MACHINE_SCHEMA_VERSION == '1'; "
            "assert pdf2pdfa.MACHINE_SCHEMA_VERSION == '1'; "
            "p=envelope('validate', ok=True, status='compliant', exit_code=0, result={}); "
            "assert p['schema_version'] == '1' and p['pdf2pdfa_version'] == pdf2pdfa.__version__; "
            "assert error_payload(FileNotFoundError('x'))['code'] == 'INPUT_NOT_FOUND'; "
            "print('installed-agent-protocol: schema v1 OK')"
        )
        _run(str(python), "-c", protocol_scan, cwd=ROOT / "scripts")

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
