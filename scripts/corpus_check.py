"""Run the owned engine against a directory of real-world PDFs.

This is release/development tooling, not runtime package code. It produces a
machine-readable report that separates already-compliant, converted, blocked
and unexpected-error cases for each requested PDF/A level.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from pdf2pdfa import Converter


DEFAULT_LEVELS = ("1b", "2b", "3b")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _pdfs(
    paths: Iterable[str],
    recursive: bool,
    *,
    excluded_roots: Iterable[Path] = (),
) -> list[Path]:
    excluded = tuple(item.resolve() for item in excluded_roots)
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() == ".pdf" and not any(_is_within(path, root) for root in excluded):
                found.add(path)
            continue
        if not path.is_dir():
            raise FileNotFoundError(path)
        iterator = path.rglob("*.pdf") if recursive else path.glob("*.pdf")
        found.update(
            item.resolve()
            for item in iterator
            if item.is_file()
            and not any(_is_within(item.resolve(), root) for root in excluded)
        )
    return sorted(found, key=lambda item: str(item).lower())


def _read_password(path: Path | None) -> str | None:
    if path is None:
        return os.environ.get("PDF2PDFA_PASSWORD")
    value = path.expanduser().read_text(encoding="utf-8")
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith(("\r", "\n")):
        return value[:-1]
    return value


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


def _font_report(report: Any | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "embedded": int(report.embedded),
        "already_embedded": int(report.already_embedded),
        "type3": int(report.type3),
        "missing": list(report.missing),
        "unsupported": list(report.unsupported),
        "complete": bool(report.complete),
    }


def _root_label(root: Path) -> str:
    base = root.name or "root"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _relative_output(source: Path, roots: list[Path], level: str, output_root: Path) -> Path:
    multi_root = len(roots) > 1
    for root in roots:
        if root.is_dir():
            try:
                relative = source.relative_to(root)
            except ValueError:
                continue
            prefix = Path(_root_label(root)) if multi_root else Path()
            return output_root / level / prefix / relative
        if root == source:
            prefix = Path(_root_label(root)) if multi_root else Path()
            return output_root / level / prefix / source.name
    return output_root / level / _root_label(source.parent) / source.name


def _case(
    source: Path,
    *,
    level: str,
    output_path: Path,
    mode: str,
    max_input_bytes: int | None,
    allow_attachment_removal: bool,
    allow_signature_invalidation: bool,
    password: str | None,
    font_paths: list[Path],
    font_directories: list[Path],
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
        inspection = converter.inspect(
            source,
            password=password,
            font_paths=font_paths,
            font_directories=font_directories,
        )
        item["source_compliant"] = bool(inspection.compliant)
        item["repairable"] = bool(inspection.repairable)
        item["validation_failures"] = _validation_failures(inspection.validation)
        item["operations"] = list(inspection.plan.operations)
        item["warnings"] = list(inspection.plan.warnings)
        item["blockers"] = _blockers(inspection.plan)
        item["encrypted"] = bool(inspection.encrypted)
        item["fonts"] = _font_report(inspection.fonts)
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
        if output_path.resolve() == source.resolve():
            raise RuntimeError("corpus output path would overwrite the source PDF")
        result = converter.convert(
            source,
            output_path,
            password=password,
            font_paths=font_paths,
            font_directories=font_directories,
        )
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
        item["result_fonts"] = _font_report(result.fonts)
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
    font_missing: dict[str, int] = {}
    for item in cases:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        for blocker in item.get("blockers", []):
            code = str(blocker.get("code", "unknown"))
            blocker_codes[code] = blocker_codes.get(code, 0) + 1
        fonts = item.get("fonts") or {}
        for name in fonts.get("missing", []):
            font_missing[str(name)] = font_missing.get(str(name), 0) + 1
        if status.endswith("error") or status == "validation-failure":
            name = str(item.get("error_type") or status)
            errors[name] = errors.get(name, 0) + 1
    return {
        "cases": len(cases),
        "statuses": dict(sorted(counts.items())),
        "blocker_codes": dict(sorted(blocker_codes.items(), key=lambda pair: (-pair[1], pair[0]))),
        "missing_fonts": dict(sorted(font_missing.items(), key=lambda pair: (-pair[1], pair[0]))),
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
    parser.add_argument("--password-file", type=Path, help="common password file for encrypted corpus PDFs")
    parser.add_argument("--font", action="append", type=Path, default=[], help="explicit TTF font; repeatable")
    parser.add_argument("--font-dir", action="append", type=Path, default=[], help="explicit font directory; repeatable")
    parser.add_argument("--max-input-mib", type=float, default=256.0)
    parser.add_argument("--allow-attachment-removal", action="store_true")
    parser.add_argument("--allow-signature-invalidation", action="store_true")
    parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="return failure if any case is explicitly fail-closed/blocked",
    )
    args = parser.parse_args()

    if args.max_input_mib is not None and args.max_input_mib <= 0:
        raise SystemExit("corpus-check: --max-input-mib must be positive")

    roots = [Path(raw).expanduser().resolve() for raw in args.paths]
    explicit_output = args.output_dir.expanduser().resolve() if args.output_dir is not None else None
    excluded_roots = [explicit_output] if explicit_output is not None else []
    sources = _pdfs(
        args.paths,
        recursive=not args.no_recursive,
        excluded_roots=excluded_roots,
    )
    if not sources:
        raise SystemExit("corpus-check: no PDF files found")

    levels = tuple(args.levels or DEFAULT_LEVELS)
    max_input_bytes = (
        int(args.max_input_mib * 1024 * 1024)
        if args.max_input_mib is not None
        else None
    )
    password = _read_password(args.password_file)
    font_paths = [path.expanduser().resolve() for path in args.font]
    font_directories = [path.expanduser().resolve() for path in args.font_dir]

    temp: tempfile.TemporaryDirectory[str] | None = None
    if explicit_output is None:
        temp = tempfile.TemporaryDirectory(prefix="pdf2pdfa-corpus-")
        output_root = Path(temp.name)
    else:
        output_root = explicit_output
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
                    password=password,
                    font_paths=font_paths,
                    font_directories=font_directories,
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

        fatal_statuses = {"inspect-error", "convert-error", "validation-failure"}
        if args.fail_on_blocked:
            fatal_statuses.add("blocked")
        bad = sum(1 for item in cases if item["status"] in fatal_statuses)
        return 1 if bad else 0
    finally:
        if temp is not None:
            temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
