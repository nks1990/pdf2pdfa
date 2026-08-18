# Changelog

All notable changes are documented here. The project follows semantic versioning for public Python/CLI behavior.

## 4.0.0

### Architecture

- Replaced the single mutation pipeline with profile-aware preflight plus adaptive backend orchestration.
- Added conservative pikepdf fast path and optional Ghostscript full-rewrite fallback.
- Added atomic publication so failed backend, validation or strict-fidelity checks preserve an existing destination.
- Added passthrough for already-valid PDFs after veraPDF confirmation.

### Compliance

- veraPDF validation now parses the requested flavour and `validationReport@isCompliant` rather than trusting process exit status or XMP claims.
- PDF/A-1b, 2b and 3b have separate profile policies.
- Added manual full-toolchain smoke testing for all supported flavours.

### Preservation fidelity

- Added optional raster fidelity reports using a controlled Ghostscript renderer and Pillow.
- Added `off`, `warn` and `strict` fidelity modes; strict mode is a publication gate.

### Fonts and color

- Unsafe Type0/CID, symbolic and custom-encoded font substitution is refused instead of guessed.
- OutputIntent and RGB/CMYK resource profiles are separated.
- ICC headers/component counts are validated before use.

### Security

- Added encrypted-PDF support with in-process temporary decryption.
- Passwords are never passed to Ghostscript command arguments.
- Added input-size limits and regular/non-empty input checks.
- Applied digital signatures are refused by default unless invalidation is explicitly allowed.

### CLI/API

- Added `preflight` command and JSON preflight output.
- Added backend selection (`auto`, `pikepdf`, `ghostscript`).
- Added fidelity controls and structured `ConversionResult` output from the Python API.
- Batch conversion now returns failure when any member fails.

### Packaging and development

- Python requirement is now 3.10+ to match the v4 type syntax actually used by the codebase.
- Removed `lxml` from runtime dependencies; it remains a test-only dependency.
- Added `fidelity` and `dev` extras and PEP 561 `py.typed` marker.
- Runtime `__version__` is derived from installed package metadata instead of being duplicated.
- Removed push/pull-request CI workflows by project policy. Manual quality gates live in `scripts/check.py` and `scripts/release_check.py`.
- PyPI release automation remains tag-only and now checks tag/version consistency, builds distributions and runs `twine check` before publication.

## 3.1.0

- Added selectable PDF/A-1b, PDF/A-2b and PDF/A-3b metadata targets.

## 3.0.0

- Added per-font system resolver, resource color-space handling, batch conversion and optional veraPDF invocation.

## 2.0.0

- Improved ICC stream metadata, CMYK handling and basic font metrics; added batch/validation CLI options.
