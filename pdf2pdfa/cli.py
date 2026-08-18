"""Command line interface for pdf2pdfa."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_LEVEL = click.Choice(["1b", "2b", "3b"], case_sensitive=False)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """pdf2pdfa - Convert PDF to PDF/A (1b, 2b, 3b)."""
    if verbose:
        logging.getLogger("pdf2pdfa").setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)


@cli.command()
@click.argument("input", type=click.Path(exists=True, dir_okay=False))
@click.argument("output", type=click.Path(dir_okay=False))
@click.option("--icc", type=click.Path(dir_okay=False), default=None, help="Path to ICC profile")
@click.option("--font", type=click.Path(dir_okay=False), default=None, help="Path to TrueType font")
@click.option("--level", type=_LEVEL, default="1b", show_default=True, help="PDF/A conformance level")
@click.option("--validate", is_flag=True, help="Require veraPDF validation after conversion")
def convert(input: str, output: str, icc: str | None, font: str | None, level: str, validate: bool) -> None:
    """Convert INPUT PDF to PDF/A OUTPUT."""
    from .converter import Converter

    conv = Converter(icc_path=icc, level=level)
    conv.convert(input, output, font_path=font)
    click.echo(f"Converted {input} -> {output} (PDF/A-{level.lower()})")

    if validate:
        _run_verapdf(output, level)


@cli.command()
@click.argument("inputs", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.option("--suffix", default="_pdfa", show_default=True, help="Suffix for output files")
@click.option("--icc", type=click.Path(dir_okay=False), default=None, help="Path to ICC profile")
@click.option("--font", type=click.Path(dir_okay=False), default=None, help="Path to TrueType font")
@click.option("--level", type=_LEVEL, default="1b", show_default=True, help="PDF/A conformance level")
@click.option("--validate", is_flag=True, help="Require veraPDF validation after each conversion")
def batch(inputs: tuple[str, ...], suffix: str, icc: str | None, font: str | None, level: str, validate: bool) -> None:
    """Convert multiple PDFs to PDF/A."""
    from .converter import Converter

    if not inputs:
        raise click.UsageError("No input files specified.")

    conv = Converter(icc_path=icc, level=level)
    failures = 0
    for inp in inputs:
        p = Path(inp)
        out = p.with_stem(p.stem + suffix)
        try:
            conv.convert(str(p), str(out), font_path=font)
            if validate:
                _run_verapdf(str(out), level)
            click.echo(f"Converted {p} -> {out}")
        except Exception as exc:
            failures += 1
            click.echo(f"FAILED {p}: {exc}", err=True)

    if failures:
        raise click.ClickException(f"{failures} conversion(s) failed")


def _run_verapdf(path: str, level: str) -> None:
    """Require veraPDF conformance for *path* and raise on failure."""
    from .validator import VeraPDFValidator, ValidatorUnavailableError

    validator = VeraPDFValidator()
    try:
        result = validator.validate(path, level)
    except ValidatorUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    if not result.compliant:
        details = f"{result.failed_checks} failed check(s)"
        if result.failed_rules:
            details += f"; rules: {', '.join(result.failed_rules[:8])}"
        raise click.ClickException(f"veraPDF: FAIL ({details})")
    click.echo(f"  veraPDF PDF/A-{result.flavour}: PASS")


if __name__ == "__main__":
    cli()
