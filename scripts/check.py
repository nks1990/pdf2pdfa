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
        help="also build wheel/sdist and run twine metadata checks",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="also run Ghostscript + veraPDF end-to-end smoke tests",
    )
    args = parser.parse_args()

    run(sys.executable, "scripts/release_check.py")
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

    if args.full:
        run(sys.executable, "scripts/e2e_smoke.py")

    print("quality-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
