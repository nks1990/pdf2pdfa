# Changelog

All notable changes to `pdf2pdfa` are documented here.

## [4.0.0] - 2026-08-18

### Breaking changes

- Replaced the old unconditional object-graph mutation pipeline with a preflight-driven conversion orchestrator.
- Unsafe font substitution is no longer attempted for Type0/CID, symbolic, custom-encoded or unsupported font structures.
- Applied digital signatures are rejected by default because conversion can invalidate them.
- veraPDF validation failures now fail the conversion instead of being treated as informational output.
- Batch conversion now exits non-zero when any item fails.
- Removed `lxml` from runtime dependencies.
- Package version is now read from installed distribution metadata instead of being duplicated in `__init__.py`.

### Added

- Profile-aware policies for PDF/A-1b, PDF/A-2b and PDF/A-3b.
- Non-mutating preflight reports covering encryption, signatures, JavaScript, attachments, transparency, fonts, color spaces, annotations, OutputIntent and existing PDF/A claims.
- Adaptive `auto`, `pikepdf` and optional `ghostscript` backends.
- Full-rewrite Ghostscript fallback for documents that cannot be repaired safely at object level.
- Atomic candidate publication: failed conversion or validation does not overwrite the destination.
- Structured veraPDF result parsing using `validationReport@isCompliant` and explicit flavours.
- Strict `validate=True` / `--validate` publication gate.
- Passthrough preservation for already-valid source PDFs after veraPDF confirms the requested flavour.
- ICC header validation and separation of OutputIntent, RGB and CMYK profile roles.
- Secure encrypted-input handling using in-process pikepdf decryption.
- `--password-file` and `PDF2PDFA_PASSWORD`; no plaintext password CLI argument.
- Optional input-size limits through `max_input_bytes` / `--max-input-mib`.
- `preflight` CLI command with human-readable and JSON output.
- Generated adversarial fixtures for transparency, JavaScript, attachments, signature fields, Type0/CID fonts and device RGB resources.
- Profile policy matrix tests, atomicity tests and CLI security contracts.
- Architecture, compliance, testing, security and contribution documentation.
- Cross-platform CI smoke tests and veraPDF jobs for 1b, 2b and 3b.
- Fail-closed PyPI release workflow with version/tag matching, package checks, Trusted Publishing and provenance attestations.

### Changed

- PDF/A-1 fast-path candidates target PDF 1.4 and disable object streams.
- Metadata conversion preserves the original creation provenance and updates modification/metadata timestamps instead of resetting creation time.
- Custom OutputIntent identifiers are no longer hard-coded to sRGB.
- README now distinguishes `UNVERIFIED` candidates from veraPDF `VERIFIED` outputs.
- Modernized package licensing metadata to PEP 639 (`MIT` SPDX expression plus `LICENSE`).

### Security

- Passwords are never forwarded to Ghostscript arguments or backend kwargs.
- Encrypted input is decrypted to a private temporary working file before external conversion.
- Signed documents require explicit destructive opt-in.
- Candidate files remain temporary until conversion and optional verification succeed.

## [3.1.0] - 2026-03-01

- Added selectable PDF/A-1b, PDF/A-2b and PDF/A-3b metadata targets.

## [3.0.0] - 2026-03-01

- Added per-font system resolution, color-space resource normalization, ICC fixes, batch conversion and optional veraPDF invocation.

## [2.0.0]

- Added initial ICC/font/color-space hardening and batch-oriented CLI features.
