# pdf2pdfa

**pdf2pdfa** converts ordinary PDF documents into fully compliant **PDF/A‑1b** files. The library offers both a simple command line tool and a Python API so that archival conversion can easily be automated or integrated into larger systems.

## Features

- Converts any PDF into valid PDF/A‑1b
- Embeds missing fonts automatically (using a fallback TrueType font)
- Attaches an sRGB ICC profile for consistent colour management
- Cleans and synchronises document metadata
- Usable from the command line or as a library

## Requirements

Python 3.7 or newer is needed. The following packages are installed automatically when using `pip`:

- [pikepdf](https://pikepdf.readthedocs.io/)
- [fonttools](https://github.com/fonttools/fonttools)
- [lxml](https://lxml.de/)
- [click](https://click.palletsprojects.com/)

For validation of the generated files you can optionally install [`verapdf`](https://github.com/veraPDF/veraPDF-library).

## Installation

Install the latest release from PyPI:

```bash
pip install pdf2pdfa
```

## Command line usage

```bash
pdf2pdfa convert input.pdf output.pdf
```

`--icc PATH` can be used to specify a custom ICC profile.

## Library usage

```python
from pdf2pdfa import Converter

conv = Converter()
conv.convert("input.pdf", "output.pdf")
```

If fonts are missing from the source PDF the converter tries to embed a common system font (e.g. DejaVu Sans). You may also supply a specific font path.

## Development and testing

Clone the repository and install the development requirements:

```bash
pip install -e .[test]
```

Run the unit tests using `pytest`. When `verapdf` is available the tests also check that the output conforms to PDF/A‑1b.

```bash
pytest
```

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.

## Contributing

Contributions are very welcome. Feel free to open issues or submit pull requests on GitHub.

