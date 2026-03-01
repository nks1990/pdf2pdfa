"""Command line interface for pdf2pdfa."""

from __future__ import annotations

import logging
import subprocess
import shutil
import sys
from pathlib import Path

import click

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """pdf2pdfa - Convert PDF to PDF/A-1b."""
    if verbose:
        logging.getLogger("pdf2pdfa").setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)


@cli.command()
@click.argument("input", type=click.Path(exists=True))
@click.argument("output", type=click.Path())
@click.option("--icc", type=click.Path(), default=None, help="Path to ICC profile")
@click.option("--font", type=click.Path(), default=None, help="Path to TrueType font")
@click.option("--validate", is_flag=True, help="Run verapdf validation after conversion")
def convert(input: str, output: str, icc: str, font: str, validate: bool) -> None:
    """Convert INPUT PDF to PDF/A-1b OUTPUT."""
    from .converter import Converter

    conv = Converter(icc_path=icc)
    conv.convert(input, output, font_path=font)
    click.echo(f"Converted {input} -> {output}")

    if validate:
        _run_verapdf(output)


@cli.command()
@click.argument("inputs", nargs=-1, type=click.Path(exists=True))
@click.option("--suffix", default="_pdfa", help="Suffix for output files (default: _pdfa)")
@click.option("--icc", type=click.Path(), default=None, help="Path to ICC profile")
@click.option("--font", type=click.Path(), default=None, help="Path to TrueType font")
@click.option("--validate", is_flag=True, help="Run verapdf validation after conversion")
def batch(inputs: tuple, suffix: str, icc: str, font: str, validate: bool) -> None:
    """Convert multiple PDFs to PDF/A-1b."""
    from .converter import Converter

    if not inputs:
        click.echo("No input files specified.", err=True)
        sys.exit(1)

    conv = Converter(icc_path=icc)
    for inp in inputs:
        p = Path(inp)
        out = p.with_stem(p.stem + suffix)
        try:
            conv.convert(str(p), str(out), font_path=font)
            click.echo(f"Converted {p} -> {out}")
            if validate:
                _run_verapdf(str(out))
        except Exception as exc:
            click.echo(f"FAILED {p}: {exc}", err=True)


def _run_verapdf(path: str) -> None:
    """Run verapdf validation on a file."""
    if shutil.which("verapdf") is None:
        click.echo("verapdf not found, skipping validation", err=True)
        return
    cmd = ["verapdf", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        click.echo(f"  verapdf: PASS")
    else:
        click.echo(f"  verapdf: FAIL", err=True)
        if result.stdout:
            click.echo(result.stdout)


if __name__ == "__main__":
    cli()
