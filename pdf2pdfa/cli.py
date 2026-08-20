"""Dependency-free command-line interface for the owned PDF/A engine."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from . import __version__
from .converter import Converter, InspectionResult
from .native.pdfa import ValidationReport


LEVELS = ("1b", "2b", "3b")
FIDELITY = ("auto", "semantic", "visual", "off")


def _password(path: str | None) -> str | None:
    if path:
        value = Path(path).read_text(encoding="utf-8")
        return value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith(("\r", "\n")) else value
    return os.environ.get("PDF2PDFA_PASSWORD")


def _max_bytes(mib: int | None) -> int | None:
    return None if mib is None else mib * 1024 * 1024


def _require_positive_mib(value: int | None) -> None:
    if value is not None and value <= 0:
        raise ValueError("--max-input-mib must be positive")


def _validation_dict(report: ValidationReport) -> dict[str, object]:
    return {
        "level": report.level,
        "compliant": report.compliant,
        "engine": report.engine,
        "passed_checks": report.passed_checks,
        "failed_checks": report.failed_checks,
        "failed_rules": list(report.failed_rules),
        "failures": [
            {
                "rule_id": failure.rule_id,
                "clause": failure.clause,
                "message": failure.message,
                "path": failure.path,
            }
            for failure in report.failures
        ],
    }


def _inspection_dict(report: InspectionResult) -> dict[str, object]:
    return {
        "level": report.level,
        "encrypted": report.encrypted,
        "compliant": report.compliant,
        "repairable": report.repairable,
        "validation": _validation_dict(report.validation),
        "plan": {
            "operations": list(report.plan.operations),
            "warnings": list(report.plan.warnings),
            "flatten_pages": list(getattr(report.plan, "flatten_pages", ())),
            "blockers": [
                {"code": item.code, "message": item.message, "path": item.path}
                for item in report.plan.blockers
            ],
        },
    }


def _add_common_conversion_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--level", choices=LEVELS, default="2b")
    parser.add_argument("--fidelity", choices=FIDELITY, default="auto")
    parser.add_argument("--password-file")
    parser.add_argument("--font", action="append", default=[], help="Owned TTF font program; repeatable")
    parser.add_argument("--font-dir", action="append", default=[], help="Directory of owned-readable TTF fonts; repeatable")
    parser.add_argument("--max-input-mib", type=int)
    parser.add_argument("--transparency-dpi", type=int, default=144)
    parser.add_argument("--visual-dpi", type=int)
    parser.add_argument("--allow-signature-invalidation", action="store_true")
    parser.add_argument("--allow-attachment-removal", action="store_true")


def _converter(args: argparse.Namespace) -> Converter:
    _require_positive_mib(args.max_input_mib)
    return Converter(
        level=args.level,
        fidelity=args.fidelity,
        max_input_bytes=_max_bytes(args.max_input_mib),
        allow_signature_invalidation=args.allow_signature_invalidation,
        allow_attachment_removal=args.allow_attachment_removal,
        transparency_dpi=args.transparency_dpi,
        visual_dpi=args.visual_dpi,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2pdfa",
        description="Convert and validate PDF/A using only the repository-owned engine.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="convert one PDF and atomically publish the result")
    convert.add_argument("input")
    convert.add_argument("output")
    convert.add_argument("--json", action="store_true", dest="json_output")
    _add_common_conversion_options(convert)

    batch = sub.add_parser("batch", help="convert multiple PDFs")
    batch.add_argument("inputs", nargs="+")
    batch.add_argument("--suffix", default="_pdfa")
    batch.add_argument("--json", action="store_true", dest="json_output")
    _add_common_conversion_options(batch)

    inspect = sub.add_parser("inspect", aliases=["preflight"], help="show owned conformance and conversion repair plan")
    inspect.add_argument("input")
    inspect.add_argument("--level", choices=LEVELS, default="2b")
    inspect.add_argument("--password-file")
    inspect.add_argument("--max-input-mib", type=int)
    inspect.add_argument("--transparency-dpi", type=int, default=144)
    inspect.add_argument("--allow-signature-invalidation", action="store_true")
    inspect.add_argument("--allow-attachment-removal", action="store_true")
    inspect.add_argument("--json", action="store_true", dest="json_output")

    validate = sub.add_parser("validate", help="validate with the owned PDF/A rule engine")
    validate.add_argument("input")
    validate.add_argument("--level", choices=LEVELS, default="2b")
    validate.add_argument("--max-input-mib", type=int)
    validate.add_argument("--json", action="store_true", dest="json_output")

    return parser


def _convert(args: argparse.Namespace) -> int:
    converter = _converter(args)
    result = converter.convert(
        args.input,
        args.output,
        password=_password(args.password_file),
        font_paths=args.font,
        font_directories=args.font_dir,
    )
    payload = {
        "output": str(result.output_path),
        "level": result.level,
        "engine": result.engine,
        "fidelity_mode": result.fidelity_mode,
        "fidelity_passed": None if result.fidelity is None else result.fidelity.passed,
        "already_compliant": result.source_was_already_compliant,
        "encrypted_input": result.source_was_encrypted,
        "validation": _validation_dict(result.validation),
        "operations": list(result.plan.operations),
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Converted {args.input} -> {args.output} "
            f"[PDF/A-{result.level}, {result.engine}, validated, fidelity={result.fidelity_mode}]"
        )
    return 0


def _batch(args: argparse.Namespace) -> int:
    converter = _converter(args)
    password = _password(args.password_file)
    results: list[dict[str, object]] = []
    failures = 0
    for item in args.inputs:
        source = Path(item)
        output = source.with_name(source.stem + args.suffix + source.suffix)
        try:
            result = converter.convert(
                source,
                output,
                password=password,
                font_paths=args.font,
                font_directories=args.font_dir,
            )
            results.append(
                {
                    "input": str(source),
                    "output": str(output),
                    "ok": True,
                    "level": result.level,
                    "fidelity_mode": result.fidelity_mode,
                }
            )
            if not args.json_output:
                print(f"OK {source} -> {output} [PDF/A-{result.level}]")
        except Exception as exc:
            failures += 1
            results.append({"input": str(source), "output": str(output), "ok": False, "error": str(exc)})
            if not args.json_output:
                print(f"FAILED {source}: {exc}", file=sys.stderr)
    if args.json_output:
        print(json.dumps({"failures": failures, "results": results}, indent=2, sort_keys=True))
    return 1 if failures else 0


def _inspect(args: argparse.Namespace) -> int:
    _require_positive_mib(args.max_input_mib)
    converter = Converter(
        level=args.level,
        max_input_bytes=_max_bytes(args.max_input_mib),
        allow_signature_invalidation=args.allow_signature_invalidation,
        allow_attachment_removal=args.allow_attachment_removal,
        transparency_dpi=args.transparency_dpi,
    )
    result = converter.inspect(args.input, password=_password(args.password_file))
    payload = _inspection_dict(result)
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        state = "COMPLIANT" if result.compliant else "NEEDS-REPAIR"
        print(f"PDF/A-{result.level}: {state}; repairable={result.repairable}; encrypted={result.encrypted}")
        for operation in result.plan.operations:
            print(f"  PLAN  {operation}")
        for blocker in result.plan.blockers:
            suffix = f" ({blocker.path})" if blocker.path else ""
            print(f"  BLOCK {blocker.code}: {blocker.message}{suffix}")
    return 0 if result.repairable or result.compliant else 2


def _validate(args: argparse.Namespace) -> int:
    _require_positive_mib(args.max_input_mib)
    report = Converter(
        level=args.level,
        max_input_bytes=_max_bytes(args.max_input_mib),
    ).validate(args.input)
    if args.json_output:
        print(json.dumps(_validation_dict(report), indent=2, sort_keys=True))
    else:
        print(f"PDF/A-{report.level}: {'PASS' if report.compliant else 'FAIL'} ({report.engine})")
        for failure in report.failures:
            suffix = f" [{failure.path}]" if failure.path else ""
            print(f"  {failure.rule_id}: {failure.message}{suffix}")
    return 0 if report.compliant else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "convert":
            return _convert(args)
        if args.command == "batch":
            return _batch(args)
        if args.command in ("inspect", "preflight"):
            return _inspect(args)
        if args.command == "validate":
            return _validate(args)
        parser.error(f"unknown command {args.command!r}")
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


cli = main  # small source-compatibility alias; no Click object remains.


if __name__ == "__main__":
    raise SystemExit(main())
