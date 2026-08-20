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

Check the installed version:

```bash
pdf2pdfa --version
python -m pdf2pdfa --version
```

## CLI

Convert one document:

```bash
pdf2pdfa convert input.pdf output.pdf --level 2b
```

The output is always validated by the owned validator before publication. There is no `--validate` switch because validation is not optional.

Dry-run the same conversion-preparation path and inspect the repair plan without writing a destination:

```bash
pdf2pdfa inspect input.pdf --level 1b
pdf2pdfa inspect input.pdf --level 1b --json
```

Inspection mirrors conversion preparation: encrypted input is authenticated/decrypted in process, signature policy is applied, explicit font programs can be simulated, the document is serialized for the requested profile, then the owned validator/repair planner runs.

For example, inspect a document while supplying the font assets that would also be supplied to conversion:

```bash
pdf2pdfa inspect input.pdf --level 2b --font ./fonts/SomeFont.ttf --json
pdf2pdfa inspect input.pdf --level 2b --font-dir ./fonts --json
```

Validate without converting:

```bash
pdf2pdfa validate file.pdf --level 2b
```

Batch conversion:

```bash
pdf2pdfa batch *.pdf --level 3b
```

For file-size ingestion limits, `convert`, `batch`, `inspect` and `validate` support `--max-input-mib` where applicable. File-backed validation/inspection checks the size before reading and rechecks after reading.

Encrypted input can use `PDF2PDFA_PASSWORD` or a password file so a secret does not need to appear in the command line:

```bash
PDF2PDFA_PASSWORD='secret' pdf2pdfa convert protected.pdf archive.pdf
pdf2pdfa convert protected.pdf archive.pdf --password-file ./password.txt
```

There is intentionally no plaintext `--password TEXT` option.

Explicit font programs can be supplied to repair missing embedded fonts without guessing against machine-specific system fonts:

```bash
pdf2pdfa convert input.pdf output.pdf --font ./fonts/SomeFont.ttf
pdf2pdfa convert input.pdf output.pdf --font-dir ./fonts
```

## Agents and headless automation

`pdf2pdfa` can be driven headlessly either through the Python API or through a subprocess-safe JSON CLI. The machine protocol is versioned independently from the package version; v5 ships **machine schema `1`**.

```bash
pdf2pdfa inspect input.pdf --level 2b --json
pdf2pdfa convert input.pdf output.pdf --level 2b --json
pdf2pdfa validate output.pdf --level 2b --json
```

Machine responses use one stable top-level envelope:

```json
{
  "schema_version": "1",
  "pdf2pdfa_version": "5.0.0",
  "ok": true,
  "status": "converted",
  "exit_code": 0,
  "command": "convert",
  "result": {}
}
```

With `--json`, normal results, validation failures, repair blockers, usage errors and runtime failures are returned as exactly one JSON document on stdout rather than requiring an agent to parse human error strings. Execution failures carry a stable `error.code`, `error.category`, concrete exception type, message and `retryable` flag.

Important statuses include `compliant`, `repairable`, `blocked`, `invalid`, `converted`, `passthrough`, `completed` and `partial_failure`. Exit codes are also part of the public contract: `0` means the requested outcome/actionable inspection succeeded, `1` is a domain-level negative result such as non-compliance or partial batch failure, `2` is a blocker/input/usage/operational failure and `130` is interruption.

The core does **not** embed an HTTP server or MCP runtime. A future remote adapter can import the Python API and reuse `pdf2pdfa.agent_protocol` without changing the zero-runtime-dependency owned engine.

See [Agent/headless integration](docs/AGENT_INTEGRATION.md) and the formal [agent protocol v1 JSON Schema](docs/agent-protocol-v1.schema.json).

## Python API

```python
from pdf2pdfa import Converter

converter = Converter(level="2b", fidelity="auto")

inspection = converter.inspect("input.pdf")
print(inspection.repairable)
print(inspection.plan.blockers)

result = converter.convert("input.pdf", "output.pdf")
print(result.validation.compliant)
print(result.validation.engine)      # pdf2pdfa-owned
print(result.fidelity_mode)          # semantic / visual / passthrough
```

Dry-run with the same explicit font program used by conversion:

```python
inspection = converter.inspect(
    "input.pdf",
    font_paths=["./fonts/SomeFont.ttf"],
)
print(inspection.fonts.embedded if inspection.fonts else 0)
```

Standalone validation:

```python
report = converter.validate("archive.pdf")
for failure in report.failures:
    print(failure.rule_id, failure.path, failure.message)
```

Applications handling untrusted uploads should set `max_input_bytes` and also apply process/container CPU, memory, wall-clock and filesystem quotas. PDF size alone cannot bound decompression/rendering cost.

## Conversion pipeline

Every rewrite follows the same fail-closed path:

1. Parse the source with the owned PDF parser.
2. Decrypt in-process when a supported Standard Security Handler password is supplied.
3. Refuse applied signatures unless invalidation was explicitly allowed.
4. Preprocess explicitly supplied font programs when required.
5. Serialize the working document for the requested PDF/A profile.
6. Run the owned validator and construct a repair plan.
7. Refuse any feature for which a safe owned repair is not implemented.
8. Apply structural repairs and, for PDF/A-1 when required, owned transparency flattening.
9. Write a candidate to a private temporary path.
10. Reparse and validate the candidate with the owned PDF/A rule engine.
11. Run semantic fidelity for structural rewrites or visual fidelity when page painting was intentionally rewritten.
12. Atomically replace the requested output only after all required gates pass.

An existing conforming, unencrypted file is preserved byte-for-byte rather than rewritten, including when it carries a signature that would otherwise be invalidated by rewriting.

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

Rewriting a signed PDF invalidates the byte ranges covered by an existing signature. A signed input that requires rewriting is therefore refused by default. Use `--allow-signature-invalidation` only when invalidating the signature is intentional. `inspect` reports the same blocker under the same policy.

## Repository layout

```text
pdf2pdfa/
  converter.py          public Python facade
  cli.py                stdlib argparse CLI
  agent_protocol.py     versioned machine envelope/error contract
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
  owned/                package/API/agent contracts
scripts/
  check.py              canonical source/package/full gate
  wheel_smoke.py        isolated installed-wheel qualification
  corpus_check.py       real-world corpus classifier/converter
  external_oracle_check.py  optional independent qualification oracles
```

See [Architecture](docs/ARCHITECTURE.md), [Compliance](docs/COMPLIANCE.md), [Testing](docs/TESTING.md), [Agent integration](docs/AGENT_INTEGRATION.md), [Renderer support](docs/RENDERER_SUPPORT.md), [Security](SECURITY.md) and [Contributing](CONTRIBUTING.md).

## Development and release checks

The repository intentionally has **no push/pull-request CI workflow**. Run the quality gate on the exact release candidate before merging or tagging:

```bash
python -m pip install -e ".[dev]"
python scripts/check.py --full
```

`--full` runs release sanity checks, package/script compilation, the complete owned regression suite, wheel/sdist build and metadata checks, an **isolated installed-wheel smoke**, and owned end-to-end conversion/validation/fidelity smoke tests for 1b, 2b and 3b. Agent JSON/schema regressions are part of the canonical owned test suite.

For release qualification beyond the generated regression corpus:

```bash
python scripts/corpus_check.py ./corpus \
  --output-dir ./qualification-output \
  --json ./qualification-output/corpus-report.json

python scripts/external_oracle_check.py ./qualification-output \
  --require verapdf \
  --json ./qualification-output/oracle-report.json
```

External tools in the second command are optional **qualification oracles** only; they are not runtime dependencies and do not participate in production conversion decisions.

The only GitHub workflow is tag-triggered PyPI publication. Its external Actions are pinned to immutable commit SHAs, it refuses a release tag whose commit is not reachable from `main`, it does not persist checkout credentials, it repeats the full owned gate and publishes only after all source/package/wheel/E2E gates pass.

## License

MIT. Compiled standards mapping data and its provenance/license notices are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
