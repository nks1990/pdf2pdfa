"""Manual quality gate used instead of push/pull-request CI."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package",
        action="store_true",
        help="also build wheel/sdist, validate metadata and smoke-test the installed wheel",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="also run owned end-to-end 1b/2b/3b conversion/validation/fidelity smoke tests",
    )
    args = parser.parse_args()

    run(sys.executable, "scripts/release_check.py")
    run(sys.executable, "-m", "compileall", "-q", "pdf2pdfa", "scripts")
    run(sys.executable, "-m", "pytest")

    if args.package or args.full:
        dist = ROOT / "dist"
        if dist.exists():
            shutil.rmtree(dist)
        run(sys.executable, "-m", "build")
        artifacts = sorted(str(path) for path in dist.iterdir() if path.is_file())
        if not artifacts:
            raise SystemExit("quality-check: build produced no dist artifacts")
        run(sys.executable, "-m", "twine", "check", *artifacts)

        wheels = sorted(path for path in dist.glob("*.whl") if path.is_file())
        if len(wheels) != 1:
            raise SystemExit(
                f"quality-check: expected exactly one wheel, found {len(wheels)}"
            )
        run(sys.executable, "scripts/wheel_smoke.py", str(wheels[0]))

    if args.full:
        # Also exercise the exact checkout used to build the release. The wheel
        # smoke above independently proves the installed artifact.
        run(sys.executable, "scripts/e2e_smoke.py")

    print("quality-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
