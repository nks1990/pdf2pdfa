# pdf2pdfa

**pdf2pdfa** is a simple tool that converts ordinary PDF files into PDF/A-1b compliant documents. It bundles a lightweight command line interface and an easy to use Python API so you can integrate archival conversion in scripts or larger applications.

## Features

- Converts existing PDFs to fully compliant PDF/A-1b.
- Automatically embeds missing fonts for reliable rendering. If a font is not
  present in the source PDF, it is replaced by DejaVu Sans and fully embedded
  with correct metrics to preserve layout.
- Attaches an sRGB ICC profile to ensure correct colour reproduction.
- Cleans and normalises document metadata.
- Provides both a CLI and a Python library.

## Installation

The package is available on PyPI:

```bash
pip install pdf2pdfa
```

## Command line usage

Convert a document directly from the terminal:

```bash
pdf2pdfa convert input.pdf output.pdf
```

You can optionally provide a custom ICC profile using `--icc PATH`.

## Library usage

```python
from pdf2pdfa import Converter

conv = Converter()
conv.convert("input.pdf", "output.pdf")
```

The converter embeds fonts and the default sRGB profile automatically. Pass an alternative profile or font path if needed.

## Development and testing

Run the unit tests with `pytest`. If the `verapdf` command line tool is installed it will also validate the generated files for PDF/A compliance.

```bash
pytest
```

Contributions are welcome! Feel free to open issues or pull requests on GitHub.

## License

This project is released under the MIT license. See the [LICENSE](LICENSE) file for details.
