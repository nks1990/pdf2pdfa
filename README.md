# pdf2pdfa

[![CI](https://github.com/nks1990/pdf2pdfa/actions/workflows/ci.yml/badge.svg)](https://github.com/nks1990/pdf2pdfa/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)

Convert ordinary PDF documents into fully compliant **PDF/A-1b** files.

## Installation

```bash
pip install pdf2pdfa
```

Requires Python 3.9+.

## CLI Usage

### Single file

```bash
pdf2pdfa convert input.pdf output.pdf
```

### Batch conversion

```bash
pdf2pdfa batch *.pdf
```

### Options

| Flag | Description |
|------|-------------|
| `--icc PATH` | Custom ICC profile |
| `--font PATH` | Custom TrueType font for embedding |
| `--validate` | Run verapdf validation after conversion |
| `-v, --verbose` | Enable debug logging |

## Python API

```python
from pdf2pdfa import Converter

conv = Converter()
conv.convert("input.pdf", "output.pdf")
```

## What it does

- **Smart font matching**: resolves each PDF font to the best system substitute by family (serif/sans/mono), weight (bold/normal), and style (italic/roman)
- Embeds missing fonts with correct WinAnsiEncoding width metrics
- Attaches sRGB ICC profile with proper `/N` on the stream dictionary
- Replaces DeviceRGB/DeviceCMYK color spaces with ICC-based equivalents
- Sets PDF/A-1b conformance in XMP metadata
- Synchronizes XMP and document info dictionary

## v3.0.0 Highlights

- **New**: Font matching system — each unembedded font is resolved individually instead of using a single fallback
  - Times-Roman → `times.ttf` (serif), Courier → `cour.ttf` (mono), Helvetica → `arial.ttf` (sans)
  - Bold, italic, and bold-italic variants are matched to the correct system font files
  - Graceful degradation: if an exact match isn't found, falls back through style → weight → category
  - Cross-platform support: Windows, macOS, and Linux font paths
  - `--font` flag still works as a user override for all fonts
- **Refactored**: `converter.py` no longer contains platform-specific font search logic

## v2.0.0 Highlights

- **Fixed**: ICC profile `/N` entry now correctly placed on stream dictionary (verapdf validation pass)
- **Fixed**: Font glyph width mismatch for WinAnsiEncoding codes 128-159
- **Fixed**: DeviceCMYK images now properly covered by CMYK OutputIntent
- **New**: `batch` command for multi-file conversion
- **New**: `--validate` flag for post-conversion verapdf check
- **New**: `--font` flag for custom font embedding
- **Removed**: `reportlab` dependency (no longer needed)
- **Removed**: Python 3.7/3.8 support (minimum 3.9)

## Development

```bash
git clone https://github.com/nks1990/pdf2pdfa.git
cd pdf2pdfa
pip install -e .[test]
pytest -v
```

## License

MIT - see [LICENSE](LICENSE).
