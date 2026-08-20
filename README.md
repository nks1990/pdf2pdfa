# pdf2pdfa

[![PyPI](https://img.shields.io/pypi/v/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![Python](https://img.shields.io/pypi/pyversions/pdf2pdfa)](https://pypi.org/project/pdf2pdfa/)
[![License](https://img.shields.io/pypi/l/pdf2pdfa)](LICENSE)

`pdf2pdfa` converts and validates **PDF/A-1b, PDF/A-2b and PDF/A-3b** using a repository-owned Python engine.

Version 5 has one architectural rule: **PDF parsing, rewriting, security handling, PDF/A validation and fidelity decisions must not be delegated to an external PDF engine or validator.**

## Ownership model

The installed package has **zero runtime dependencies**.

It does not invoke or import an external PDF converter, validator, rasterizer, font engine, image library or CLI framework. The runtime implementation lives in `pdf2pdfa/native/` and uses the Python standard library only.

The owned engine contains its own:

- PDF tokenizer, COS objects, classic xref/xref-stream parser, object-stream reader and incremental-update handling;
- PDF writer and deterministic classic-xref output path;
- stream filters and predictors;
- Standard Security Handler including RC4/AES revisions implemented in Python;
- content-stream parser;
- PDF/A-1b/2b/3b rule engine;
- XMP/ICC generation and validation;
- TrueType/CID, CFF1, Type1 and Type3 font parsing/rendering paths;
- horizontal and vertical Type0 text-state support;
- owned predefined-CMap registry with `Identity-H`/`Identity-V` and compiled Adobe-Japan1 `90ms-RKSJ-H`/`90ms-RKSJ-V` mappings;
- color-space and ICC transformations;
- baseline JPEG and CCITT Group 3/4/mixed fax decoding;
- vector rasterizer, page renderer, patterns and shading types 1-7;
- isolated and RGB non-isolated transparency compositing plus bounded knockout support;
- annotation appearance rendering and PDF/A-1 appearance repair;
- PDF/A-1 transparency flattening;
- semantic and visual fidelity gates;
- atomic publication.

`tests/owned/test_package_ownership.py` enforces the runtime boundary by scanning every distributed Python module and rejecting non-stdlib imports.

## Installation

```bash
pip install pdf2pdfa
```

Python 3.10+ is required. No additional runtime package or executable is required.

## CLI

Convert one document:

```bash
pdf2pdfa convert input.pdf output.pdf --level 2b
```

The output is always validated by the owned validator before publication. There is no `--validate` switch because validation is not optional.

Inspect a document and the exact repair plan without writing it:

```bash
pdf2pdfa inspect input.pdf --level 1b
pdf2pdfa inspect input.pdf --level 1b --json
```

Validate without converting:

```bash
pdf2pdfa validate file.pdf --level 2b
```

Batch conversion:

```bash
pdf2pdfa batch *.pdf --level 3b
```

Encrypted input can use `PDF2PDFA_PASSWORD` or a password file so a secret does not need to appear in the command line:

```bash
PDF2PDFA_PASSWORD='secret' pdf2pdfa convert protected.pdf archive.pdf
pdf2pdfa convert protected.pdf archive.pdf --password-file ./password.txt
```

Explicit font programs can be supplied to repair missing embedded fonts without guessing against machine-specific system fonts:

```bash
pdf2pdfa convert input.pdf output.pdf --font ./fonts/SomeFont.ttf
pdf2pdfa convert input.pdf output.pdf --font-dir ./fonts
```

## Python API

```python
from pdf2pdfa import Converter

converter = Converter(level="2b", fidelity="auto")

inspection = converter.inspect("input.pdf")
print(inspection.repairable)

result = converter.convert("input.pdf", "output.pdf")
print(result.validation.compliant)
print(result.validation.engine)      # pdf2pdfa-owned
print(result.fidelity_mode)          # semantic / visual / passthrough
```

Standalone validation:

```python
report = converter.validate("archive.pdf")
for failure in report.failures:
    print(failure.rule_id, failure.path, failure.message)
```

## Conversion pipeline

Every rewrite follows the same fail-closed path:

1. Parse the source with the owned PDF parser.
2. Decrypt in-process when a supported Standard Security Handler password is supplied.
3. Refuse applied signatures unless invalidation was explicitly allowed.
4. Preprocess explicit font programs when required.
5. Run the owned validator and construct a repair plan.
6. Refuse any feature for which a safe owned repair is not implemented.
7. Apply structural repairs and, for PDF/A-1 when required, owned transparency flattening.
8. Write a candidate to a private temporary path.
9. Reparse and validate the candidate with the owned PDF/A rule engine.
10. Run semantic fidelity for structural rewrites or visual fidelity when page painting was intentionally rewritten.
11. Atomically replace the requested output only after all required gates pass.

An existing conforming, unencrypted file is copied byte-for-byte rather than rewritten.

## Fidelity

`fidelity="auto"` is the default.

- **semantic** compares page geometry, decoded content, text-show instructions, images and attachments where a rewrite is expected not to change painting;
- **visual** renders source and candidate through the same owned raster pipeline and compares RGB output in memory;
- **auto** uses semantic fidelity for structural repair and automatically switches to visual fidelity for operations such as PDF/A-1 transparency flattening;
- **off** disables the fidelity gate, but never disables PDF/A validation.

## PDF/A profiles

| Profile | Engine policy |
|---|---|
| PDF/A-1b | PDF 1.4 output, classic xref, no encryption, no JavaScript, no embedded files, no forbidden transparency; supported transparency is flattened by the owned renderer. |
| PDF/A-2b | PDF 1.7 feature model with PDF/A restrictions and controlled embedded-file rules. |
| PDF/A-3b | PDF 1.7 feature model with PDF/A-3 associated-file handling. |

The validator reports structured failures with rule id, clause, message and PDF object/path. A candidate with any unresolved validation failure is never published.

## Fail-closed coverage

Owning the engine also means not pretending an unimplemented decoder or painting primitive is safe. The converter raises an explicit blocker when a required transformation cannot yet be proven by owned code.

The production-reachable renderer currently covers vector paths/clipping, affine and pattern strokes, TrueType/CIDFontType2, CFF1/CIDFontType0C, embedded Type1, Type3, horizontal/vertical Type0 text, device/ICC color, raw/filtered images, baseline JPEG, CCITT fax, masks/soft masks, blend modes, shading types 1-7, PatternType 1/2, isolated transparency groups, RGB non-isolated groups, bounded knockout rendering and normal annotation appearances.

Remaining explicit renderer blockers include:

- JPX / JPEG 2000 image decoding;
- JBIG2 image decoding;
- additional Adobe predefined non-Identity CMap families beyond the currently compiled Japan1 `90ms-RKSJ-H` / `90ms-RKSJ-V` mappings;
- explicit non-RGB transparency-group blending spaces;
- advanced knockout interactions where the renderer cannot yet preserve PDF object shape/opacity semantics exactly;
- annotation display geometry that depends on `NoZoom`, `NoRotate` or `ToggleNoView`.

These are **fail-closed** limitations. If conversion or fidelity requires an unsupported path, pdf2pdfa rejects the operation instead of approximating it or delegating it to external software. See [Renderer support](docs/RENDERER_SUPPORT.md) for the detailed matrix.

## Digital signatures

Rewriting a signed PDF invalidates the byte ranges covered by an existing signature. Signed input is therefore refused by default. Use `--allow-signature-invalidation` only when invalidating the signature is intentional.

## Repository layout

```text
pdf2pdfa/
  converter.py          public Python facade
  cli.py                stdlib argparse CLI
  native/
    document.py         parser / object resolution
    writer.py           PDF writer
    filters.py          stream codecs
    security.py         Standard Security Handler
    content.py          content stream parser
    pdfa.py             PDF/A rule engine
    repair.py           structural repair planner/engine
    repair_owned.py     rendering-aware repair
    pipeline.py         canonical conversion orchestration
    raster.py           owned raster surface/path engine
    page_render.py      PDF page interpreter
    owned_renderer.py   production renderer composition
    transparency_render.py
    visual_fidelity.py
    ...
tests/
  native/               component and adversarial regression tests
  owned/                package ownership and public API contracts
```

See [Architecture](docs/ARCHITECTURE.md), [Compliance](docs/COMPLIANCE.md), [Testing](docs/TESTING.md), [Renderer support](docs/RENDERER_SUPPORT.md), [Security](SECURITY.md) and [Contributing](CONTRIBUTING.md).

## Development and release checks

The repository intentionally has **no push/pull-request CI workflow**. Run the quality gate before merging or tagging:

```bash
python -m pip install -e ".[dev]"
python scripts/check.py --full
```

`--full` runs release sanity checks, the complete owned regression suite, package build/metadata checks and owned end-to-end conversion/validation/fidelity smoke tests for 1b, 2b and 3b.

The only GitHub workflow is tag-triggered PyPI publication. The release workflow repeats the full owned gate and publishes only if it passes, so a release tag cannot bypass tests, package checks or end-to-end smoke validation.

## License

MIT. Compiled standards mapping data and its provenance/license notices are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
