# pdf2pdfa

[![PyPI](https://img.shields.io/pypi/v/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![Python](https://img.shields.io/pypi/pyversions/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![License](https://img.shields.io/pypi/l/pdf2pdfa)](LICENSE)

`pdf2pdfa` is a Python library and CLI for converting PDFs to **PDF/A-1b, PDF/A-2b, or PDF/A-3b** with a conservative, profile-aware pipeline.

Version 4 is designed around one rule: **do not claim compliance merely because PDF/A metadata was written**. The converter preflights the input, chooses the least destructive backend that can safely handle it, can require independent veraPDF validation before publication, and can optionally compare the rendered output against the source.

## Installation

```bash
pip install pdf2pdfa
```

Python 3.10+ is required.

For visual fidelity checking:

```bash
pip install "pdf2pdfa[fidelity]"
```

### Optional external tools

- **veraPDF** — required only when `--validate` / `validate=True` is used. A failed validation blocks publication.
- **Ghostscript** — used by the full-rewrite fallback for difficult inputs and by visual fidelity rendering. It is intentionally not bundled; install and license it separately for your environment.
- **Pillow** — installed by the `fidelity` extra and used to compare rendered pages.

## Quick start

### CLI

```bash
pdf2pdfa convert input.pdf output.pdf --level 2b
```

Production-oriented conversion with both independent compliance validation and visual drift protection:

```bash
pdf2pdfa convert input.pdf output.pdf \
  --level 2b \
  --backend auto \
  --validate \
  --fidelity strict
```

Inspect a document without changing it:

```bash
pdf2pdfa preflight input.pdf --level 1b
pdf2pdfa preflight input.pdf --level 1b --json-output
```

Batch conversion:

```bash
pdf2pdfa batch *.pdf --level 3b --validate
```

### Python API

```python
from pdf2pdfa import Converter

converter = Converter(
    level="2b",
    backend="auto",
    validate=True,
    fidelity="strict",
)

report = converter.preflight("input.pdf")
result = converter.convert("input.pdf", "output.pdf")

print(result.backend)
print(result.validation.compliant if result.validation else None)
print(result.fidelity.passed if result.fidelity else None)
```

## Conversion model

The v4 pipeline is deliberately adaptive:

1. **Preflight** — detects signatures, encryption, JavaScript/actions, embedded files, transparency, font complexity, device color spaces and existing PDF/A claims.
2. **Plan** — selects the conservative pikepdf fast path when object-level repair is considered safe, otherwise selects the Ghostscript rewrite backend.
3. **Convert** — works in a private temporary area. Encrypted inputs are decrypted in-process; passwords are never forwarded to Ghostscript.
4. **Validate** — when requested, veraPDF validates the exact requested flavour (`1b`, `2b`, `3b`). A non-compliant candidate is not published.
5. **Fidelity** — optional `warn` or `strict` raster comparison checks page count, geometry and rendered appearance.
6. **Atomic publish** — only the final candidate is moved into the requested output path. A failed conversion does not destroy an existing output file.

### Backend choices

- `auto` — recommended. Uses pikepdf when safe and falls back to Ghostscript when a full rewrite is required or when the fast-path candidate fails validation.
- `pikepdf` — conservative object-level normalization only. Unsafe Type0/CID/custom-encoded font repair is refused instead of guessed.
- `ghostscript` — forces full PDF/A rewrite through the external Ghostscript executable.

## PDF/A profiles

| Profile | Supported | Notes |
|---|---:|---|
| PDF/A-1b | Yes | Transparency and embedded files require repair/removal according to the profile policy. |
| PDF/A-2b | Yes | Allows transparency; uses the v4 profile-aware pipeline. |
| PDF/A-3b | Yes | Supports the broader PDF/A-3 feature model, including embedded files. |

`pdf2pdfa` distinguishes a **conversion attempt** from a **verified result**. If your workflow requires a compliance guarantee, use `--validate` and treat veraPDF as the authority.

## Fonts

The pikepdf fast path only embeds/replaces fonts when the mapping is demonstrably simple and safe. It does **not** silently rewrite Type0/CID, symbolic, Type3, or custom-encoded fonts as WinAnsi TrueType. Inputs requiring glyph-preserving reconstruction are routed to the full-rewrite backend in `auto` mode.

## Color management

OutputIntent and resource color-space profiles are separate concepts in v4. ICC profiles are parsed and checked for component count; an arbitrary CMYK profile can no longer be accidentally reused as an RGB ICCBased color space. Bundled RGB/CMYK profiles are used for matching device-space resources.

## Encrypted and signed PDFs

Encrypted PDFs are supported when a password is supplied. For CLI use, avoid putting secrets in shell arguments:

```bash
PDF2PDFA_PASSWORD='secret' pdf2pdfa convert protected.pdf output.pdf --validate
```

or:

```bash
pdf2pdfa convert protected.pdf output.pdf --password-file ./password.txt
```

Digital signatures are different: conversion changes the PDF byte stream and can invalidate an existing signature. Signed PDFs are therefore refused by default. Override this only when invalidation is intentional with `--allow-signature-invalidation` or the equivalent Python option.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Compliance model](docs/COMPLIANCE.md)
- [Testing strategy](docs/TESTING.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Development

The repository intentionally has **no push/pull-request CI workflow**. Run the full checks locally before merging or releasing:

```bash
git clone https://github.com/nks1990/pdf2pdfa.git
cd pdf2pdfa
python -m pip install -e ".[dev]"
pytest
python -m build
python -m twine check dist/*
```

For end-to-end compliance testing, install veraPDF and Ghostscript and run the integration tests in an environment where both executables are available.

## Release policy

PyPI publication is isolated from continuous integration. The GitHub release workflow is tag-triggered and only builds/publishes a release; ordinary pushes and pull requests do not run workflows.

Before tagging a release, verify tests, package build, `twine check`, veraPDF conversion for all supported flavours, and fidelity smoke tests manually.

## License

MIT for `pdf2pdfa` itself. External tools such as Ghostscript and veraPDF have their own licenses and are not bundled with this package.
