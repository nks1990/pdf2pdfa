"""Run the owned engine against a directory of real-world PDFs.

This is release/development tooling, not runtime package code. It produces a
machine-readable report that separates already-compliant, converted, blocked
and unexpected-error cases for each requested PDF/A level.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable

from pdf2pdfa import Converter


DEFAULT_LEVELS = ("1b", "2b", "3b")


def _pdfs(paths: Iterable[str], recursive: bool) -> list[Path]:
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() == ".pdf":
                found.add(path)
            continue
        if not path.is_dir():
            raise FileNotFoundError(path)
        iterator = path.rglob("*.pdf") if recursive else path.glob("*.pdf")
        found.update(item.resolve() for item in iterator if item.is_file())
    return sorted(found, key=lambda item: str(item).lower())


def _validation_failures(report: Any, limit: int = 50) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for item in list(report.failures)[:limit]:
        output.append(
            {
                "rule_id": str(item.rule_id),
                "clause": str(item.clause),
                "path": str(item.path),
                "message": str(item.message),
            }
        )
    return output


def _blockers(plan: Any) -> list[dict[str, str]]:
    return [
        {
            "code": str(item.code),
            "path": str(item.path),
            "message": str(item.message),
        }
        for item in plan.blockers
    ]


def _relative_output(source: Path, roots: list[Path], level: str, output_root: Path) -> Path:
    for root in roots:
        if root.is_dir():
            try:
                relative = source.relative_to(root)
                return output_root / level / relative
            except ValueError:
                pass
    return output_root / level / source.name


def _case(
    source: Path,
    *,
    level: str,
    output_path: Path,
    mode: str,
    max_input_bytes: int | None,
    allow_attachment_removal: bool,
    allow_signature_invalidation: bool,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source": str(source),
        "level": level,
        "status": "unknown",
        "output": None,
    }
    converter = Converter(
        level=level,
        fidelity="auto",
        max_input_bytes=max_input_bytes,
        allow_attachment_removal=allow_attachment_removal,
        allow_signature_invalidation=allow_signature_invalidation,
    )

    try:
        inspection = converter.inspect(source)
        item["source_compliant"] = bool(inspection.compliant)
        item["repairable"] = bool(inspection.repairable)
        item["validation_failures"] = _validation_failures(inspection.validation)
        item["operations"] = list(inspection.plan.operations)
        item["warnings"] = list(inspection.plan.warnings)
        item["blockers"] = _blockers(inspection.plan)
        item["encrypted"] = bool(inspection.encrypted)
    except Exception as exc:
        item["status"] = "inspect-error"
        item["error_type"] = type(exc).__name__
        item["error"] = str(exc)
        return item

    if mode == "inspect":
        if inspection.compliant:
            item["status"] = "compliant"
        elif inspection.repairable:
            item["status"] = "repairable"
        else:
            item["status"] = "blocked"
        return item

    if not inspection.repairable:
        item["status"] = "blocked"
        return item

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = converter.convert(source, output_path)
        item["status"] = "converted"
        item["output"] = str(output_path)
        item["result_compliant"] = bool(result.validation.compliant)
        item["engine"] = str(result.engine)
        item["fidelity_mode"] = str(result.fidelity_mode)
        item["source_was_already_compliant"] = bool(result.source_was_already_compliant)
        item["source_was_encrypted"] = bool(result.source_was_encrypted)
        item["result_validation_failures"] = _validation_failures(result.validation)
        item["result_operations"] = list(result.plan.operations)
        item["result_blockers"] = _blockers(result.plan)
        if not result.validation.compliant:
            item["status"] = "validation-failure"
        return item
    except Exception as exc:
        item["status"] = "convert-error"
        item["error_type"] = type(exc).__name__
        item["error"] = str(exc)
        return item


def _summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    blocker_codes: dict[str, int] = {}
    errors: dict[str, int] = {}
    for item in cases:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        for blocker in item.get("blockers", []):
            code = str(blocker.get("code", "unknown"))
            blocker_codes[code] = blocker_codes.get(code, 0) + 1
        if status.endswith("error") or status == "validation-failure":
            name = str(item.get("error_type") or status)
            errors[name] = errors.get(name, 0) + 1
    return {
        "cases": len(cases),
        "statuses": dict(sorted(counts.items())),
        "blocker_codes": dict(sorted(blocker_codes.items(), key=lambda pair: (-pair[1], pair[0]))),
        "errors": dict(sorted(errors.items(), key=lambda pair: (-pair[1], pair[0]))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="PDF file(s) or corpus directories")
    parser.add_argument(
        "--level",
        action="append",
        choices=DEFAULT_LEVELS,
        dest="levels",
        help="target level; repeat for multiple levels (default: 1b,2b,3b)",
    )
    parser.add_argument("--mode", choices=("inspect", "convert"), default="convert")
    parser.add_argument("--no-recursive", action="store_true", help="do not recurse into corpus directories")
    parser.add_argument("--output-dir", type=Path, help="keep converted outputs under this directory")
    parser.add_argument("--json", dest="json_path", type=Path, help="write full JSON report")
    parser.add_argument("--max-input-mib", type=float, default=256.0)
    parser.add_argument("--allow-attachment-removal", action="store_true")
    parser.add_argument("--allow-signature-invalidation", action="store_true")
    args = parser.parse_args()

    roots = [Path(raw).expanduser().resolve() for raw in args.paths]
    sources = _pdfs(args.paths, recursive=not args.no_recursive)
    if not sources:
        raise SystemExit("corpus-check: no PDF files found")

    levels = tuple(args.levels or DEFAULT_LEVELS)
    max_input_bytes = int(args.max_input_mib * 1024 * 1024) if args.max_input_mib else None

    temp: tempfile.TemporaryDirectory[str] | None = None
    if args.output_dir is None:
        temp = tempfile.TemporaryDirectory(prefix="pdf2pdfa-corpus-")
        output_root = Path(temp.name)
    else:
        output_root = args.output_dir.expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)

    cases: list[dict[str, Any]] = []
    try:
        for index, source in enumerate(sources, start=1):
            for level in levels:
                output_path = _relative_output(source, roots, level, output_root)
                print(f"[{index}/{len(sources)}] PDF/A-{level} {source}", flush=True)
                result = _case(
                    source,
                    level=level,
                    output_path=output_path,
                    mode=args.mode,
                    max_input_bytes=max_input_bytes,
                    allow_attachment_removal=args.allow_attachment_removal,
                    allow_signature_invalidation=args.allow_signature_invalidation,
                )
                cases.append(result)
                print(f"  -> {result['status']}", flush=True)

        report = {
            "levels": list(levels),
            "mode": args.mode,
            "source_files": len(sources),
            "summary": _summary(cases),
            "cases": cases,
        }

        if args.json_path is not None:
            json_path = args.json_path.expanduser().resolve()
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"report: {json_path}")

        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))

        bad = sum(
            1
            for item in cases
            if item["status"] in {"inspect-error", "convert-error", "validation-failure"}
        )
        return 1 if bad else 0
    finally:
        if temp is not None:
            temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
