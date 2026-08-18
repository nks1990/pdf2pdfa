# PDF/A compliance model

`pdf2pdfa` distinguishes **conversion**, **standards conformance verification**, and **visual fidelity verification**.

A file is not considered verified merely because it contains `pdfaid:part` and `pdfaid:conformance` metadata. When validation is enabled, an external veraPDF result for the requested flavour is the conformance gate. Visual fidelity is a separate optional gate and cannot substitute for standards validation.

## Supported targets

| Target | Transparency | Embedded arbitrary files | JavaScript | Encryption |
|---|---:|---:|---:|---:|
| PDF/A-1b | no | no | no | no |
| PDF/A-2b | yes | no | no | no |
| PDF/A-3b | yes | yes | no | no |

The table describes the policy enforced by the current converter. A feature being permitted by a profile does not by itself prove that every representation of that feature is conformant; veraPDF remains the standards oracle when validation is requested.

## Verified versus unverified output

### Verified

Created with `validate=True` or CLI `--validate` and accepted by veraPDF for the exact requested flavour.

Properties:

- the candidate was validated before publication;
- validation failure leaves an existing destination untouched;
- an already-valid source can be copied unchanged instead of rewritten;
- automatic mode may retry with the full-rewrite backend if the fast-path candidate fails validation.

### Unverified

Created without veraPDF validation. This mode exists for environments where veraPDF is unavailable or where callers deliberately separate conversion and validation.

An unverified output is a **PDF/A candidate**, not a library guarantee of standards compliance. The CLI labels it `UNVERIFIED` for this reason.

## Fidelity modes

Visual fidelity answers a different question from PDF/A validation: whether the rendered result materially changed relative to the source.

- `fidelity="off"` — no raster comparison;
- `fidelity="warn"` — compare pages and return the report, but do not block publication;
- `fidelity="strict"` — page-count, page-size or rendered-difference failures block atomic publication.

The checker renders source and candidate through the same Ghostscript raster pipeline and compares the resulting images using bounded pixel tolerances. This detects many visible regressions, but it does not prove semantic equivalence of text, links, annotations or embedded files.

## Font policy

The object-level backend embeds missing fonts only when the original mapping can be preserved conservatively.

It refuses to rewrite the following as generic WinAnsi TrueType fonts:

- Type0/CID fonts;
- custom encodings;
- Symbol and ZapfDingbats substitutions;
- unsupported font subtypes;
- arbitrary unknown font families unless the caller explicitly provides a simple-font override.

These cases are routed to a full rewrite in automatic mode or fail when the pikepdf backend is forced.

## Color policy

OutputIntent and resource color profiles have separate roles.

- custom OutputIntent ICC profiles are validated structurally;
- RGB replacement profiles must declare three components;
- CMYK replacement profiles must declare four components;
- the library does not describe a custom CMYK profile as sRGB;
- resource normalization is intentionally conservative and does not pretend to be a full color-management engine.

Difficult color transformations belong to the full-rewrite backend and still require validation.

## PDF/A-1

PDF/A-1 has the strictest feature constraints among the supported targets. Automatic mode routes transparency and similar features to the full rewrite path rather than attempting to relabel a PDF 1.5+ object graph as PDF/A-1.

The pikepdf fast path saves PDF/A-1 candidates at PDF 1.4 compatibility and disables object streams.

## PDF/A-2

PDF/A-2 permits features such as transparency that PDF/A-1 does not. Embedded arbitrary files are still treated as a profile blocker.

## PDF/A-3

PDF/A-3 permits embedded files. The converter therefore does not automatically classify their mere presence as an error for a `3b` target.

Preserving attachment semantics through a full rewrite can be backend-dependent. For workflows where attachments are business-critical, use validation and independently test that the expected attachments remain present.

## Existing PDF/A files

When validation is enabled and the source claims the requested profile, `pdf2pdfa` validates the source first. If veraPDF confirms compliance, the source is copied byte-for-byte instead of being needlessly transformed.

Without validation, an existing XMP claim is not trusted enough to trigger passthrough.

## Digital signatures

A structurally applied digital signature is treated separately from an empty signature form field.

Any conversion that rewrites bytes can invalidate a signature. Signed PDFs are rejected by default. The caller must explicitly opt into signature invalidation before conversion is attempted.

## Test strategy

Compliance-related testing is intentionally split into four layers:

1. **unit and structural tests** for policies, parsing and object-graph decisions;
2. **generated adversarial fixtures** for transparency, JavaScript, attachments, signatures, Type0/CID fonts and device color spaces;
3. **veraPDF integration validation** as the standards conformance gate;
4. **visual fidelity comparison** as an independent preservation oracle.

The repository intentionally does not run these on push or pull request. Before release, `python scripts/check.py --full` runs the complete manual gate. A passing unit test suite without veraPDF integration is not treated as proof of PDF/A compliance.
