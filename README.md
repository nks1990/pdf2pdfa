# pdf2pdfa

`pdf2pdfa` converts PDF files into PDF/A-1b compliant documents.

## Installation

```bash
pip install pdf2pdfa
```

## Command Line Usage

```bash
pdf2pdfa convert input.pdf output.pdf
```

Use `--icc PATH` to supply a custom ICC color profile.

## Library Usage

```python
from pdf2pdfa import Converter

conv = Converter()
conv.convert('input.pdf', 'output.pdf')
```

## Testing

Tests require the `verapdf` CLI to validate PDF/A compliance:

```bash
pytest
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
