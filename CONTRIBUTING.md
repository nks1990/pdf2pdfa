# Contributing

Thank you for improving `pdf2pdfa`.

The project handles an archival format where a plausible-looking output is not enough. Changes should optimize for preservation, explicit failure and externally verifiable conformance.

## Development setup

```bash
python -m venv .venv
# activate the environment for your platform
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python scripts/check.py
```

There is intentionally no push/pull-request CI workflow. Contributors are responsible for running the manual quality gate before opening or merging a change.

For release-level integration testing, install Ghostscript and veraPDF and run:

```bash
python scripts/check.py --full
```

## Pull-request expectations

A change should normally include:

- a focused explanation of the PDF/PDF-A behavior being changed;
- regression tests for the affected structure;
- no silent weakening of validator, preflight or fidelity policy;
- documentation changes when public behavior changes;
- an explicit note when fidelity or compatibility tradeoffs are involved;
- the manual check command(s) that were run.

## PDF fixtures

Prefer small, programmatically generated fixtures in `tests/fixtures.py` when a PDF feature can be represented that way. This keeps the repository auditable and lets reviewers see exactly which objects create the test condition.

A binary fixture is appropriate when the bug depends on a real producer, malformed structure, encoding or renderer behavior that cannot be reproduced faithfully with a small generator. Before committing a binary fixture:

1. remove private or identifying content;
2. minimize the file where possible;
3. document its origin and redistribution permission;
4. explain exactly which regression it covers.

Never add confidential customer documents.

## Conformance changes

Do not add support for a PDF/A level by changing only XMP metadata.

A new or modified profile needs a policy definition, preflight rules, backend behavior, a veraPDF flavour test and documentation of allowed/forbidden features.

`validationReport@isCompliant` is the conformance decision. A veraPDF process exit code alone is not sufficient.

## Fidelity changes

Standards compliance and preservation fidelity are separate gates. Changes that can affect rendering should add or update visual-fidelity tests. Do not loosen default tolerances merely to hide a regression; document why a tolerance change is technically justified.

## Font changes

Never guess a complex character-code-to-glyph mapping. Type0/CID, symbolic and custom-encoded fonts require a glyph-preserving implementation or a full rewrite backend.

If a proposed font fix makes a test PDF look better by changing its encoding, it must also prove that text semantics and glyph selection remain correct.

## Color changes

Keep OutputIntent profiles separate from source/resource color-space profiles. Validate ICC component counts before using a profile in an ICCBased color space.

Do not describe assignment of an ICC profile as a color conversion unless pixel/content values are actually transformed under color management.

## Security-sensitive changes

Do not put passwords or other secrets in subprocess arguments, logs, exception messages or test snapshots. Preserve atomic publication behavior. Signed documents must remain opt-in for destructive conversion.

## Backward compatibility

Public API compatibility matters, but correctness takes precedence over preserving behavior that silently corrupts or falsely certifies PDFs. Breaking changes should be documented and released under an appropriate major version.
