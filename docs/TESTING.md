# Testing

The test suite is layered because no single test technique can prove both PDF/A conformance and document fidelity.

## 1. Unit tests

Unit tests cover deterministic internal contracts:

- PDF/A profile policy;
- ICC parsing and component counts;
- font-name parsing and safe/unsafe embedding decisions;
- veraPDF XML parsing;
- backend selection;
- password and input-size handling;
- atomic publication behavior.

These tests should run without Ghostscript or veraPDF unless they explicitly target an integration boundary.

## 2. Generated structural fixtures

`tests/fixtures.py` creates small PDFs that isolate relevant structures:

- transparency via ExtGState;
- JavaScript actions;
- embedded-file name trees;
- applied and empty signature fields;
- Type0/CID font dictionaries;
- DeviceRGB image resources.

The profile matrix verifies that the same structure can be accepted or rejected differently by PDF/A-1b, 2b and 3b.

Generated fixtures are not substitutes for real-world PDFs. They exist to make the policy state machine deterministic and reviewable.

## 3. Real-world regression corpus

When a bug depends on a specific producer or malformed file, add a minimized and redistributable fixture with provenance notes. Useful producer classes include:

- Microsoft Office and LibreOffice;
- browser print engines;
- CAD/GIS software;
- ReportLab and other PDF libraries;
- scanners and OCR tools;
- signed government/enterprise forms;
- PDFs with CJK/CID fonts;
- mixed RGB/CMYK print documents.

Never commit confidential documents.

## 4. veraPDF integration

The CI compliance job must validate an actual converted PDF with veraPDF using an explicit flavour.

For example:

```bash
verapdf --format xml --flavour 1b output.pdf
```

The test must parse `validationReport@isCompliant`. Do not infer compliance only from the process exit code.

For new profile behavior, add at least one positive and one negative validation case when practical.

## 5. Fidelity testing

veraPDF proves standards rules, not visual or semantic equivalence to the source. High-value regressions should therefore add one or more fidelity oracles where possible:

- extracted text comparison;
- page count and geometry comparison;
- attachment inventory comparison for PDF/A-3;
- rendered-page comparison with a controlled rasterizer;
- font/glyph checks for encoding-sensitive cases.

Visual comparison needs tolerances and must not hide structural failures. A visually identical rasterized PDF may have lost searchable text, links, annotations or attachments.

## Atomicity tests

Every backend and validation failure path should preserve an existing destination. Tests intentionally place sentinel bytes at the output path and assert they survive failure.

## Security tests

Encrypted-input tests should prove all of the following:

- missing/wrong password fails clearly;
- a correct password permits preflight;
- the temporary backend input is unencrypted;
- the password is absent from backend kwargs and command arguments;
- optional size limits are enforced before expensive parsing.

## Cross-platform testing

Platform-specific behavior matters primarily for:

- font discovery;
- Ghostscript executable discovery;
- filesystem atomic replacement;
- path quoting and temporary files.

CI should retain Linux coverage across supported Python versions and add smoke coverage on Windows and macOS.

## Before release

A release candidate should satisfy:

1. unit/structural suite passes;
2. veraPDF integration passes for supported target flavours exercised by CI;
3. package builds cleanly;
4. wheel and sdist metadata are checked;
5. version and Git tag match;
6. README limitations and external-tool requirements are current;
7. no known failing regression is hidden by `skip` unless the skip is explicitly justified.
