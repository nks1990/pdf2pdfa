# Testing

The test strategy is layered because no single technique can prove both PDF/A conformance and preservation fidelity. The repository intentionally has no push/pull-request CI workflow; these checks are run manually before merging and releasing.

## Fast quality gate

Install development dependencies and run:

```bash
python -m pip install -e ".[dev]"
python scripts/check.py
```

This runs the release-layout sanity check and the complete pytest suite.

## Package gate

```bash
python scripts/check.py --package
```

In addition to tests, this rebuilds `dist/` from scratch and runs `twine check` on wheel and sdist metadata.

## Full external-tool gate

With Ghostscript and veraPDF available:

```bash
python scripts/check.py --full
```

The full gate includes package checks and forces real Ghostscript conversions for PDF/A-1b, PDF/A-2b and PDF/A-3b, then requires veraPDF compliance for each result. Visual fidelity is also exercised when the optional Pillow stack is installed.

## 1. Unit tests

Unit tests cover deterministic internal contracts:

- PDF/A profile policy;
- ICC parsing and component counts;
- font-name parsing and safe/unsafe embedding decisions;
- veraPDF XML parsing;
- backend selection;
- password and input-size handling;
- atomic publication behavior;
- fidelity thresholds and page-count drift.

These tests do not require Ghostscript or veraPDF unless they explicitly target an integration boundary.

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

When a bug depends on a specific producer or malformed file, add a minimized and redistributable fixture with provenance notes. Useful producer classes include Microsoft Office/LibreOffice, browsers, CAD/GIS software, PDF libraries, scanners/OCR tools, signed forms, CJK/CID documents and mixed RGB/CMYK print documents.

Never commit confidential documents.

## 4. veraPDF integration

Compliance tests must validate an actual converted PDF with an explicit flavour and inspect `validationReport@isCompliant`. Do not infer conformance only from the process exit code.

Example:

```bash
verapdf --format xml --flavour 1b output.pdf
```

`scripts/e2e_smoke.py` exercises all three supported flavours through the real conversion stack.

## 5. Fidelity testing

veraPDF proves standards rules, not visual or semantic equivalence to the source. `pdf2pdfa.fidelity.VisualFidelityChecker` therefore renders both PDFs through the same Ghostscript rasterizer and compares page count, dimensions, mean error and changed-pixel ratio.

`fidelity="warn"` reports drift without blocking publication. `fidelity="strict"` turns the comparison into an atomic publication gate.

Visual comparison complements rather than replaces structural checks: a visually identical rasterized PDF may still have lost searchable text, links, annotations or attachments.

## Atomicity tests

Every backend, validation and strict-fidelity failure path must preserve an existing destination. Tests place sentinel bytes at the output path and assert they survive failure.

## Security tests

Encrypted-input tests prove that missing/wrong passwords fail clearly, correct passwords permit preflight, temporary backend input is unencrypted, passwords do not enter backend arguments, and optional size limits are enforced before expensive parsing.

## Cross-platform manual smoke testing

Before a significant release, test at least one supported Python on Windows, macOS and Linux when practical. Platform-sensitive areas are font discovery, Ghostscript executable discovery, filesystem atomic replacement, subprocess path handling and temporary files.

## Before release

A release candidate should satisfy all of the following:

1. `python scripts/check.py --full` passes in an environment with Ghostscript and veraPDF;
2. wheel and sdist build successfully and pass `twine check`;
3. `python scripts/release_check.py` passes and the release tag matches `v<project.version>`;
4. README limitations and external-tool requirements are current;
5. no known regression is hidden by an unjustified skip;
6. `.github/workflows/` contains only the tag-triggered release workflow.
