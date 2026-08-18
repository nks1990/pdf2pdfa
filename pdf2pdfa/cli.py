"""Command line interface for pdf2pdfa."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import click

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

_LEVEL = click.Choice(["1b", "2b", "3b"], case_sensitive=False)
_BACKEND = click.Choice(["auto", "pikepdf", "ghostscript"], case_sensitive=False)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Convert PDFs to PDF/A with preflight, adaptive backends and validation."""
    if verbose:
        logging.getLogger("pdf2pdfa").setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)


def _converter(
    *,
    level: str,
    icc: str | None,
    backend: str,
    validate: bool,
    allow_signature_invalidation: bool,
    ghostscript: str | None,
    verapdf: str,
):
    from .converter import Converter

    return Converter(
        icc_path=icc,
        level=level,
        backend=backend.lower(),
        validate=validate,
        allow_signature_invalidation=allow_signature_invalidation,
        ghostscript_executable=ghostscript,
        verapdf_executable=verapdf,
    )


@cli.command("preflight")
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.option("--level", type=_LEVEL, default="1b", show_default=True)
@click.option("--json-output", is_flag=True, help="Emit machine-readable JSON")
def preflight_cmd(input: str, level: str, json_output: bool) -> None:
    """Inspect INPUT without modifying it."""
    from .preflight import analyze_pdf

    report = analyze_pdf(input, level)
    if json_output:
        payload = {
            "level": report.level,
            "features": report.features,
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity.value,
                    "message": issue.message,
                    "repairable": issue.repairable,
                    "context": issue.context,
                }
                for issue in report.issues
            ],
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    click.echo(f"PDF/A-{report.level} preflight: {input}")
    if not report.issues:
        click.echo("  no conversion blockers detected")
    for issue in report.issues:
        click.echo(f"  {issue.severity.value.upper():7} {issue.code}: {issue.message}")


@cli.command()
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.argument("output", type=click.Path(dir_okay=False))
@click.option("--icc", type=click.Path(dir_okay=False), default=None, help="OutputIntent ICC profile")
@click.option("--font", type=click.Path(dir_okay=False), default=None, help="Explicit font override for safe simple-font embedding")
@click.option("--level", type=_LEVEL, default="1b", show_default=True, help="PDF/A conformance level")
@click.option("--backend", type=_BACKEND, default="auto", show_default=True)
@click.option("--validate", is_flag=True, help="Require veraPDF compliance before publishing OUTPUT")
@click.option("--allow-signature-invalidation", is_flag=True, help="Explicitly allow conversion of signed PDFs")
@click.option("--ghostscript", type=click.Path(dir_okay=False), default=None, help="Ghostscript executable override")
@click.option("--verapdf", default="verapdf", show_default=True, help="veraPDF executable")
def convert(
    input: str,
    output: str,
    icc: str | None,
    font: str | None,
    level: str,
    backend: str,
    validate: bool,
    allow_signature_invalidation: bool,
    ghostscript: str | None,
    verapdf: str,
) -> None:
    """Convert INPUT PDF to PDF/A OUTPUT."""
    conv = _converter(
        level=level,
        icc=icc,
        backend=backend,
        validate=validate,
        allow_signature_invalidation=allow_signature_invalidation,
        ghostscript=ghostscript,
        verapdf=verapdf,
    )
    try:
        result = conv.convert(input, output, font_path=font)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    status = "VERIFIED" if result.validation is not None else "UNVERIFIED"
    fallback = ", fallback" if result.fallback_used else ""
    click.echo(
        f"Converted {input} -> {output} (PDF/A-{result.level}, {result.backend}{fallback}, {status})"
    )


@cli.command()
@click.argument("inputs", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.option("--suffix", default="_pdfa", show_default=True, help="Suffix for output files")
@click.option("--icc", type=click.Path(dir_okay=False), default=None, help="OutputIntent ICC profile")
@click.option("--font", type=click.Path(dir_okay=False), default=None)
@click.option("--level", type=_LEVEL, default="1b", show_default=True)
@click.option("--backend", type=_BACKEND, default="auto", show_default=True)
@click.option("--validate", is_flag=True, help="Require veraPDF compliance for every output")
@click.option("--allow-signature-invalidation", is_flag=True)
@click.option("--ghostscript", type=click.Path(dir_okay=False), default=None)
@click.option("--verapdf", default="verapdf", show_default=True)
def batch(
    inputs: tuple[str, ...],
    suffix: str,
    icc: str | None,
    font: str | None,
    level: str,
    backend: str,
    validate: bool,
    allow_signature_invalidation: bool,
    ghostscript: str | None,
    verapdf: str,
) -> None:
    """Convert multiple PDFs; return non-zero if any file fails."""
    if not inputs:
        raise click.UsageError("No input files specified.")

    conv = _converter(
        level=level,
        icc=icc,
        backend=backend,
        validate=validate,
        allow_signature_invalidation=allow_signature_invalidation,
        ghostscript=ghostscript,
        verapdf=verapdf,
    )
    failures = 0
    for inp in inputs:
        source = Path(inp)
        output = source.with_stem(source.stem + suffix)
        try:
            result = conv.convert(source, output, font_path=font)
            status = "VERIFIED" if result.validation else "UNVERIFIED"
            click.echo(f"Converted {source} -> {output} [{result.backend}, {status}]")
        except Exception as exc:
            failures += 1
            click.echo(f"FAILED {source}: {exc}", err=True)

    if failures:
        raise click.ClickException(f"{failures} conversion(s) failed")


if __name__ == "__main__":
    cli()
