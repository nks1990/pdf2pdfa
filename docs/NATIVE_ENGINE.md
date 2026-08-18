# Native PDF/A engine

`pdf2pdfa` owns the conversion and validation logic for its supported PDF/A profiles.

## Ownership rule

The runtime must not invoke external PDF conversion, validation or rendering executables. In particular:

- no Ghostscript conversion backend;
- no veraPDF validation subprocess;
- no external rasterizer used as a correctness gate.

Python libraries may be used as low-level building blocks, but PDF/A policy, rule evaluation, conversion planning, mutation, publication and correctness decisions live in this repository.

## Native pipeline

```text
input
  -> structural parser / preflight
  -> native rule engine
  -> native repair planner
  -> native object-level transformations
  -> native rule engine again
  -> semantic fidelity invariants
  -> atomic publish
```

The validator is fail-closed: a rule that cannot be evaluated must not be silently treated as passing.

## Supported profiles

The first native target set remains PDF/A-1b, PDF/A-2b and PDF/A-3b. Each profile owns a concrete rule set rather than sharing one conversion path with different XMP labels.

## Conversion policy

Safe native repairs include removal of forbidden interactive actions, profile-aware attachment handling, metadata synchronization, output intents, encryption removal, conservative font embedding, resource color normalization and PDF-version/object-stream controls.

Transformations that require changing page appearance or glyph semantics are admitted only when a native implementation can prove the result. Until such implementation exists, the engine must return an explicit unsupported-feature error rather than delegate to another executable or emit a false PDF/A claim.

## Validation policy

Rules are represented as code with stable rule identifiers, profile applicability, severity and evidence. Validation produces structured results and can be used as an atomic publication gate without any external validator.

## Fidelity policy

The native fidelity gate is based on invariants we control: page count, boxes/rotation, page content-stream identity when a repair must not alter painting operators, font/glyph mapping constraints, attachment inventory for PDF/A-3, and metadata-independent object checks. A future native renderer may add raster comparison, but external renderers are not part of the correctness contract.
