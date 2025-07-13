"""Command line interface for pdf2pdfa."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from .converter import Converter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """pdf2pdfa CLI"""


@cli.command()
@click.argument('input', type=click.Path(exists=True))
@click.argument('output', type=click.Path())
@click.option('--icc', type=click.Path(), default=None, help='Path to ICC profile')
def convert(input: str, output: str, icc: str) -> None:
    """Convert INPUT PDF to PDF/A-1b OUTPUT."""
    conv = Converter(icc_path=icc)
    conv.convert(input, output)
    click.echo(f'Converted {input} \u2192 {output} as PDF/A-1b')


if __name__ == '__main__':
    cli()

