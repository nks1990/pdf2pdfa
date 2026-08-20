"""Compare release outputs with independent PDF qualification tools.

This script is intentionally outside the runtime package. External tools are
optional release oracles only; production conversion/validation never imports
or executes them.

Expected corpus layout from ``scripts/corpus_check.py --output-dir OUT``::

    OUT/1b/...pdf
    OUT/2b/...pdf
    OUT/3b/...pdf

veraPDF conformance is determined from validationReport@isCompliant, not from
its process exit code. qpdf and Ghostscript are used as structural/rendering
smoke oracles when available.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from xml.etree import ElementTree as ET

from pdf2pdfa import Converter


LEVELS = {"1b", "2b", "3b"}


def _which(explicit: str | None, candidates: tuple[str, ...]) -> str | None:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(explicit)
        return found
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s: {command[0]}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot execute {command[0]}: {exc}") from exc


def _verapdf(path: Path, level: str, executable: str, timeout: int) -> dict[str, Any]:
    result = _run(
        [executable, "--format", "xml", "--flavour", level, str(path)],
        timeout,
    )
    xml_text = result.stdout.strip()
    if not xml_text:
        raise RuntimeError(result.stderr.strip() or f"veraPDF exited {result.returncode}")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"veraPDF returned invalid XML: {exc}") from exc

    report = None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "validationReport":
            report = node
            break
    if report is None:
        raise RuntimeError("veraPDF XML does not contain validationReport")

    compliant = report.attrib.get("isCompliant", "false").lower() == "true"
    failed_checks = 0
    passed_checks = 0
    failed_rules: list[str] = []
    for node in report.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "details":
            failed_checks = int(
                node.attrib.get("failedChecks", node.attrib.get("failedRules", "0")) or 0
            )
            passed_checks = int(node.attrib.get("passedChecks", "0") or 0)
        elif local == "rule" and node.attrib.get("status", "").lower() == "failed":
            rule = (
                node.attrib.get("specification")
                or node.attrib.get("clause")
                or node.attrib.get("testNumber")
            )
            if rule:
                failed_rules.append(rule)

    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or f"veraPDF exited {result.returncode}")

    return {
        "compliant": compliant,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "failed_rules": list(dict.fromkeys(failed_rules)),
        "returncode": result.returncode,
    }


def _qpdf(path: Path, executable: str, timeout: int) -> dict[str, Any]:
    result = _run([executable, "--check", str(path)], timeout)
    # qpdf uses 0 for clean, 3 for warnings and 2 for errors.
    return {
        "ok": result.returncode in (0, 3),
        "warnings": result.returncode == 3,
        "returncode": result.returncode,
        "stdout": result.stdout.strip()[-4000:],
        "stderr": result.stderr.strip()[-4000:],
    }


def _ghostscript(path: Path, executable: str, timeout: int) -> dict[str, Any]:
    result = _run(
        [
            executable,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=nullpage",
            str(path),
        ],
        timeout,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip()[-4000:],
        "stderr": result.stderr.strip()[-4000:],
    }


def _level_for(path: Path, root: Path, explicit_level: str | None) -> str:
    if explicit_level:
        return explicit_level
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"cannot infer level for {path}") from exc
    if not relative.parts or relative.parts[0].lower() not in LEVELS:
        raise ValueError(
            f"cannot infer PDF/A level for {path}; expected first path component 1b/2b/3b"
        )
    return relative.parts[0].lower()


def _files(root: Path, explicit_level: str | None) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(root)
    if explicit_level:
        return sorted(item.resolve() for item in root.rglob("*.pdf") if item.is_file())
    output: list[Path] = []
    for level in sorted(LEVELS):
        directory = root / level
        if directory.is_dir():
            output.extend(item.resolve() for item in directory.rglob("*.pdf") if item.is_file())
    return sorted(set(output), key=lambda item: str(item).lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="PDF or corpus output root")
    parser.add_argument("--level", choices=sorted(LEVELS), help="use one level for all input PDFs")
    parser.add_argument("--verapdf", help="veraPDF executable/path")
    parser.add_argument("--qpdf", help="qpdf executable/path")
    parser.add_argument("--ghostscript", help="Ghostscript executable/path")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument(
        "--require",
        action="append",
        choices=("verapdf", "qpdf", "ghostscript"),
        default=[],
        help="fail immediately if this oracle is unavailable; repeat as needed",
    )
    args = parser.parse_args()

    root = args.path.expanduser().resolve()
    files = _files(root, args.level)
    if not files:
        raise SystemExit("external-oracle-check: no PDF files found")

    executables = {
        "verapdf": _which(args.verapdf, ("verapdf",)),
        "qpdf": _which(args.qpdf, ("qpdf",)),
        "ghostscript": _which(args.ghostscript, ("gs", "gswin64c", "gswin32c")),
    }
    missing_required = [name for name in args.require if executables[name] is None]
    if missing_required:
        raise SystemExit(
            "external-oracle-check: missing required oracle(s): " + ", ".join(missing_required)
        )

    cases: list[dict[str, Any]] = []
    critical = 0
    for index, path in enumerate(files, start=1):
        level = _level_for(path, root if root.is_dir() else path.parent, args.level)
        print(f"[{index}/{len(files)}] PDF/A-{level} {path}", flush=True)
        item: dict[str, Any] = {"path": str(path), "level": level}

        try:
            owned = Converter(level=level).validate(path)
            item["owned"] = {
                "compliant": bool(owned.compliant),
                "passed_checks": int(owned.passed_checks),
                "failed_checks": int(owned.failed_checks),
                "failed_rules": list(owned.failed_rules),
            }
        except Exception as exc:
            item["owned_error"] = {"type": type(exc).__name__, "message": str(exc)}
            critical += 1

        if executables["verapdf"]:
            try:
                item["verapdf"] = _verapdf(path, level, executables["verapdf"], args.timeout)
            except Exception as exc:
                item["verapdf_error"] = {"type": type(exc).__name__, "message": str(exc)}
                critical += 1

        if executables["qpdf"]:
            try:
                item["qpdf"] = _qpdf(path, executables["qpdf"], args.timeout)
                if not item["qpdf"]["ok"]:
                    critical += 1
            except Exception as exc:
                item["qpdf_error"] = {"type": type(exc).__name__, "message": str(exc)}
                critical += 1

        if executables["ghostscript"]:
            try:
                item["ghostscript"] = _ghostscript(
                    path, executables["ghostscript"], args.timeout
                )
                if not item["ghostscript"]["ok"]:
                    critical += 1
            except Exception as exc:
                item["ghostscript_error"] = {"type": type(exc).__name__, "message": str(exc)}
                critical += 1

        owned_compliant = item.get("owned", {}).get("compliant")
        vera_compliant = item.get("verapdf", {}).get("compliant")
        if owned_compliant is not None and vera_compliant is not None:
            item["validator_agreement"] = owned_compliant == vera_compliant
            if not item["validator_agreement"]:
                critical += 1
        print(
            "  owned={} vera={} qpdf={} gs={}".format(
                owned_compliant,
                vera_compliant if "verapdf" in item else "n/a",
                item.get("qpdf", {}).get("ok", "n/a"),
                item.get("ghostscript", {}).get("ok", "n/a"),
            ),
            flush=True,
        )
        cases.append(item)

    summary = {
        "files": len(files),
        "critical_findings": critical,
        "oracles": {name: bool(path) for name, path in executables.items()},
        "validator_disagreements": sum(
            1 for item in cases if item.get("validator_agreement") is False
        ),
        "qpdf_failures": sum(1 for item in cases if item.get("qpdf", {}).get("ok") is False),
        "ghostscript_failures": sum(
            1 for item in cases if item.get("ghostscript", {}).get("ok") is False
        ),
    }
    report = {"summary": summary, "cases": cases}

    if args.json_path:
        json_path = args.json_path.expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report: {json_path}")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
