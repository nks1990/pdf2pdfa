"""Dependency-free command-line interface for the owned PDF/A engine."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from . import __version__
from .agent_protocol import envelope, error_payload
from .converter import Converter, InspectionResult
from .native.pdfa import ValidationReport


LEVELS = ("1b", "2b", "3b")
FIDELITY = ("auto", "semantic", "visual", "off")
_COMMANDS = {"convert", "batch", "inspect", "preflight", "validate"}


class CLIUsageError(ValueError):
    """Raised instead of argparse printing an unstructured usage error."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


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
    fonts = None
    if report.fonts is not None:
        fonts = {
            "embedded": report.fonts.embedded,
            "already_embedded": report.fonts.already_embedded,
            "type3": report.fonts.type3,
            "missing": list(report.fonts.missing),
            "unsupported": list(report.fonts.unsupported),
            "complete": report.fonts.complete,
        }
    return {
        "level": report.level,
        "encrypted": report.encrypted,
        "compliant": report.compliant,
        "repairable": report.repairable,
        "validation": _validation_dict(report.validation),
        "fonts": fonts,
        "plan": {
            "operations": list(report.plan.operations),
            "warnings": list(report.plan.warnings),
            "flatten_pages": list(getattr(report.plan, "flatten_pages", ())),
            "flatten_annotation_pages": list(
                getattr(report.plan, "flatten_annotation_pages", ())
            ),
            "blockers": [
                {"code": item.code, "message": item.message, "path": item.path}
                for item in report.plan.blockers
            ],
        },
    }


def _json_print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _command_name(value: str | None) -> str | None:
    return "inspect" if value == "preflight" else value


def _command_hint(argv: Sequence[str]) -> str | None:
    for item in argv:
        if item in _COMMANDS:
            return _command_name(item)
    return argv[0] if argv and not argv[0].startswith("-") else None


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
    parser = _ArgumentParser(
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

    inspect = sub.add_parser("inspect", aliases=["preflight"], help="dry-run owned conversion preparation and show its repair plan")
    inspect.add_argument("input")
    inspect.add_argument("--level", choices=LEVELS, default="2b")
    inspect.add_argument("--password-file")
    inspect.add_argument("--font", action="append", default=[], help="Owned TTF font program; repeatable")
    inspect.add_argument("--font-dir", action="append", default=[], help="Directory of owned-readable TTF fonts; repeatable")
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
        status = "passthrough" if result.source_was_already_compliant else "converted"
        _json_print(
            envelope(
                "convert",
                ok=True,
                status=status,
                exit_code=0,
                result=payload,
            )
        )
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
                    "status": "passthrough" if result.source_was_already_compliant else "converted",
                    "level": result.level,
                    "fidelity_mode": result.fidelity_mode,
                }
            )
            if not args.json_output:
                print(f"OK {source} -> {output} [PDF/A-{result.level}]")
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "input": str(source),
                    "output": str(output),
                    "ok": False,
                    "status": error_payload(exc)["category"],
                    "error": error_payload(exc),
                }
            )
            if not args.json_output:
                print(f"FAILED {source}: {exc}", file=sys.stderr)
    code = 1 if failures else 0
    if args.json_output:
        _json_print(
            envelope(
                "batch",
                ok=failures == 0,
                status="partial_failure" if failures else "completed",
                exit_code=code,
                result={"failures": failures, "results": results},
            )
        )
    return code


def _inspect(args: argparse.Namespace) -> int:
    _require_positive_mib(args.max_input_mib)
    converter = Converter(
        level=args.level,
        max_input_bytes=_max_bytes(args.max_input_mib),
        allow_signature_invalidation=args.allow_signature_invalidation,
        allow_attachment_removal=args.allow_attachment_removal,
        transparency_dpi=args.transparency_dpi,
    )
    result = converter.inspect(
        args.input,
        password=_password(args.password_file),
        font_paths=args.font,
        font_directories=args.font_dir,
    )
    payload = _inspection_dict(result)
    if result.compliant:
        status, code, ok = "compliant", 0, True
    elif result.repairable:
        status, code, ok = "repairable", 0, True
    else:
        status, code, ok = "blocked", 2, False
    if args.json_output:
        _json_print(
            envelope(
                "inspect",
                ok=ok,
                status=status,
                exit_code=code,
                result=payload,
            )
        )
    else:
        state = "COMPLIANT" if result.compliant else "NEEDS-REPAIR"
        print(f"PDF/A-{result.level}: {state}; repairable={result.repairable}; encrypted={result.encrypted}")
        if result.fonts is not None:
            print(
                "  FONTS "
                f"embedded={result.fonts.embedded} already={result.fonts.already_embedded} "
                f"missing={len(result.fonts.missing)} unsupported={len(result.fonts.unsupported)}"
            )
        for operation in result.plan.operations:
            print(f"  PLAN  {operation}")
        for blocker in result.plan.blockers:
            suffix = f" ({blocker.path})" if blocker.path else ""
            print(f"  BLOCK {blocker.code}: {blocker.message}{suffix}")
    return code


def _validate(args: argparse.Namespace) -> int:
    _require_positive_mib(args.max_input_mib)
    report = Converter(
        level=args.level,
        max_input_bytes=_max_bytes(args.max_input_mib),
    ).validate(args.input)
    payload = _validation_dict(report)
    code = 0 if report.compliant else 1
    if args.json_output:
        _json_print(
            envelope(
                "validate",
                ok=report.compliant,
                status="compliant" if report.compliant else "invalid",
                exit_code=code,
                result=payload,
            )
        )
    else:
        print(f"PDF/A-{report.level}: {'PASS' if report.compliant else 'FAIL'} ({report.engine})")
        for failure in report.failures:
            suffix = f" [{failure.path}]" if failure.path else ""
            print(f"  {failure.rule_id}: {failure.message}{suffix}")
    return code


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "convert":
        return _convert(args)
    if args.command == "batch":
        return _batch(args)
    if args.command in ("inspect", "preflight"):
        return _inspect(args)
    if args.command == "validate":
        return _validate(args)
    raise CLIUsageError(f"unknown command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    command = _command_hint(raw)
    parser = _parser()
    try:
        args = parser.parse_args(raw)
    except CLIUsageError as exc:
        if json_requested:
            _json_print(
                envelope(
                    command,
                    ok=False,
                    status="usage_error",
                    exit_code=2,
                    error={
                        "code": "USAGE_ERROR",
                        "type": type(exc).__name__,
                        "category": "usage_error",
                        "message": str(exc),
                        "retryable": False,
                    },
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    command = _command_name(args.command)
    json_requested = bool(getattr(args, "json_output", False))
    try:
        return _dispatch(args)
    except KeyboardInterrupt as exc:
        if json_requested:
            _json_print(
                envelope(
                    command,
                    ok=False,
                    status="interrupted",
                    exit_code=130,
                    error=error_payload(exc),
                )
            )
        else:
            print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if json_requested:
            details = error_payload(exc)
            _json_print(
                envelope(
                    command,
                    ok=False,
                    status=str(details["category"]),
                    exit_code=2,
                    error=details,
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


cli = main


if __name__ == "__main__":
    raise SystemExit(main())
