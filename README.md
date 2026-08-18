# pdf2pdfa

[![CI](https://github.com/nks1990/pdf2pdfa/actions/workflows/ci.yml/badge.svg)](https://github.com/nks1990/pdf2pdfa/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![Python](https://img.shields.io/pypi/pyversions/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![License](https://img.shields.io/pypi/l/pdf2pdfa)](LICENSE)

Adaptive PDF-to-PDF/A conversion for **PDF/A-1b, PDF/A-2b and PDF/A-3b**, with profile-aware preflight, conservative object-level repair, an optional full-rewrite backend, and veraPDF-gated output when strict verification is requested.

`pdf2pdfa` deliberately distinguishes **creating a PDF/A candidate** from **proving conformance**. Use `--validate` (or `validate=True`) when the output must be accepted by veraPDF for the exact requested flavour before it is published.

## Why v4 is different

PDF/A is not an XMP flag. Real inputs can contain encryption, JavaScript, transparency, embedded files, complex CID fonts, device-dependent color spaces, signatures and other structures whose handling changes by PDF/A profile.

The v4 pipeline therefore does this:

```text
PDF -> security checks -> preflight -> safe fast path / full rewrite
    -> candidate -> optional veraPDF gate -> atomic output
```

Key properties:

- **Profile-aware**: PDF/A-1b, 2b and 3b have different policies.
- **Adaptive**: pikepdf is used for conservative repairs; Ghostscript can be used for full rewrites.
- **Font-safe**: Type0/CID, symbolic and custom-encoded fonts are never silently rewritten as generic WinAnsi fonts.
- **Color-safe**: OutputIntent, RGB and CMYK ICC roles are kept separate and ICC component counts are validated.
- **Signature-aware**: applied digital signatures are blocked by default because rewriting can invalidate them.
- **Password-safe**: encrypted PDFs are decrypted in-process; passwords are never forwarded to Ghostscript command-line arguments.
- **Atomic**: a failed backend or validation never replaces an existing destination with a partial candidate.
- **Externally verifiable**: strict mode parses veraPDF's `validationReport@isCompliant` for the requested flavour.

## Installation

```bash
pip install pdf2pdfa
```

Python **3.9+** is supported.

The Python package does not bundle Ghostscript or veraPDF:

- **Ghostscript** is optional and is used by `backend=auto` when a difficult PDF requires a full rewrite. It is separately distributed and licensed by Artifex.
- **veraPDF** is optional for ordinary candidate generation and required for strict `--validate` verification.

If an optional executable is needed but unavailable, `pdf2pdfa` fails explicitly instead of pretending the conversion succeeded.

## Quick start

### Inspect before converting

```bash
pdf2pdfa preflight input.pdf --level 1b
```

Machine-readable preflight:

```bash
pdf2pdfa preflight input.pdf --level 2b --json-output
```

### Convert a PDF

```bash
pdf2pdfa convert input.pdf output.pdf --level 2b
```

Without `--validate`, the CLI reports the result as `UNVERIFIED`.

### Require veraPDF verification

```bash
pdf2pdfa convert input.pdf output.pdf --level 2b --validate
```

In strict mode the destination is published only if veraPDF accepts the requested PDF/A flavour.

### Batch conversion

```bash
pdf2pdfa batch *.pdf --level 3b --validate
```

The batch command exits non-zero if any conversion or validation fails.

## Backend selection

The default is `auto`:

```bash
pdf2pdfa convert input.pdf output.pdf --backend auto
```

Available choices:

| Backend | Purpose |
|---|---|
| `auto` | Recommended. Preflight chooses the safe path and can fall back after a failed candidate. |
| `pikepdf` | Conservative object-level fast path. Refuses structures it cannot repair safely. |
| `ghostscript` | Forces the optional full-rewrite backend. |

A forced `pikepdf` conversion fails if preflight proves that a full rewrite is required.

## Encrypted PDFs

Do not put passwords directly on a command line. `pdf2pdfa` intentionally has no `--password TEXT` option.

Use a password file:

```bash
pdf2pdfa convert protected.pdf output.pdf --password-file password.txt --validate
```

or the environment:

```bash
export PDF2PDFA_PASSWORD='secret'
pdf2pdfa convert protected.pdf output.pdf --validate
```

On Windows PowerShell:

```powershell
$env:PDF2PDFA_PASSWORD = 'secret'
pdf2pdfa convert protected.pdf output.pdf --validate
```

The password is consumed by pikepdf in-process. External conversion backends receive only a temporary unencrypted working PDF.

## Signed PDFs

Applied digital signatures are rejected by default because any conversion can invalidate the signed byte ranges.

If invalidation is intentional:

```bash
pdf2pdfa convert signed.pdf output.pdf --allow-signature-invalidation --validate
```

An empty signature form field is not treated as an applied signature.

## Resource limits

Services processing untrusted uploads can reject unexpectedly large inputs before parsing:

```bash
pdf2pdfa convert input.pdf output.pdf --max-input-mib 250
```

This is a basic guard, not a full sandbox. Production services should also enforce process CPU, memory, time and filesystem limits.

## Custom ICC profile

```bash
pdf2pdfa convert input.pdf output.pdf --icc /path/to/profile.icc --validate
```

The profile is structurally validated before it becomes the OutputIntent. A custom CMYK OutputIntent is not reused as an RGB replacement profile.

## Font override

```bash
pdf2pdfa convert input.pdf output.pdf --font /path/to/font.ttf
```

The override exists for explicit simple-font workflows. It does **not** make unsafe Type0/CID or symbolic dictionary substitution acceptable; complex mappings are still routed away from the object-level fast path.

## Python API

```python
from pdf2pdfa import Converter

converter = Converter(
    level="2b",
    backend="auto",
    validate=True,
)

report = converter.preflight("input.pdf")
result = converter.convert("input.pdf", "output.pdf")

print(result.backend)
print(result.validation.compliant if result.validation else "unverified")
```

Encrypted input:

```python
result = converter.convert(
    "protected.pdf",
    "output.pdf",
    password="secret",
)
```

Useful constructor options include:

```python
Converter(
    level="1b",
    backend="auto",
    validate=False,
    allow_signature_invalidation=False,
    ghostscript_executable=None,
    verapdf_executable="verapdf",
    timeout=300,
    max_input_bytes=None,
)
```

## Supported targets

| Target | Transparency | Arbitrary embedded files | JavaScript | Encryption |
|---|---:|---:|---:|---:|
| PDF/A-1b | no | no | no | no |
| PDF/A-2b | yes | no | no | no |
| PDF/A-3b | yes | yes | no | no |

These are conversion policies, not a substitute for validator results. See [docs/COMPLIANCE.md](docs/COMPLIANCE.md) for the detailed model.

## Validation semantics

### `UNVERIFIED`

A conversion completed and produced a PDF/A candidate, but no veraPDF gate was requested.

### `VERIFIED`

veraPDF accepted the exact requested flavour before the destination was atomically published.

If the source already claims the requested profile and strict validation confirms that claim, `pdf2pdfa` preserves the source without needlessly rewriting it.

## Known boundaries

PDF is a large format and no converter can safely infer missing semantics in every malformed document. `pdf2pdfa` therefore prefers explicit failure over silent corruption.

Important boundaries:

- veraPDF proves PDF/A rules, not visual equivalence to the source;
- a full rewrite can change non-archival interactive behavior;
- PDF/A-3 attachment preservation should be independently checked when attachments are business-critical;
- digital-signature validation is outside this project's scope;
- `pdf2pdfa` is not an antivirus scanner, redaction tool or malware sandbox;
- hostile PDFs should be processed with operating-system resource isolation in server environments.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Compliance model](docs/COMPLIANCE.md)
- [Testing strategy](docs/TESTING.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## Development

```bash
git clone https://github.com/nks1990/pdf2pdfa.git
cd pdf2pdfa
python -m venv .venv
pip install -e ".[dev]"
pytest -v
ruff check pdf2pdfa tests
```

CI additionally exercises supported Python versions, Windows/macOS smoke tests, package building, and veraPDF checks for PDF/A-1b, 2b and 3b.

## License

`pdf2pdfa` itself is MIT licensed. See [LICENSE](LICENSE).

Optional external tools are not bundled and retain their own licenses. In particular, review Artifex's Ghostscript licensing terms for your intended use before deploying the Ghostscript backend.
