# Architecture

`pdf2pdfa` v4 is built around one rule: a PDF must never be mutated blindly just because a PDF/A metadata marker can be written into it.

## Pipeline

```text
input PDF
   |
   v
filesystem/security checks
   |
   v
profile-aware preflight
   |
   +---------------------------+
   |                           |
   v                           v
safe object-level path      full rewrite path
(pikepdf)                   (Ghostscript)
   |                           |
   +-------------+-------------+
                 |
                 v
            candidate PDF
                 |
          optional veraPDF
          conformance gate
                 |
          optional raster
          fidelity gate
                 |
                 v
        atomic publication
```

The public `Converter` facade delegates to `ConversionOrchestrator`. The orchestrator owns policy and publication; backends only create candidates.

## Modules

### `profiles.py`

Defines the supported PDF/A policies. PDF/A-1b, PDF/A-2b and PDF/A-3b are represented as different policies, not as different XMP strings on an otherwise identical transformation.

### `preflight.py`

Performs a non-mutating structural inspection. It records or detects encryption, applied digital signatures, JavaScript actions, embedded files, transparency, annotations/appearance resources, unembedded fonts, Type0/CID and other complex fonts, device-dependent color resources, an existing OutputIntent and an existing PDF/A XMP claim.

Preflight findings are data (`PreflightReport`), not log messages. Backend selection consumes those findings.

### `backends/pikepdf_backend.py`

The conservative fast path. It is chosen only when the object graph can be repaired without changing the meaning of character codes or requiring a rendered rewrite.

It may embed a missing simple WinAnsi font when the mapping is demonstrably safe, embed/describe ICC profiles, normalize explicit RGB/CMYK resource references, update PDF/A metadata without falsifying the original creation date, and save with a PDF version compatible with the selected profile.

It must refuse transformations that would require guessing.

### `backends/ghostscript.py`

The optional full-rewrite path for inputs requiring reconstruction or rendering, such as PDF/A-1 transparency flattening or unsafe font structures. Ghostscript is an external dependency and is not bundled with this MIT-licensed Python package.

A generated PDF/A definition embeds a validated binary ICC profile and an OutputIntent. The backend writes only to a temporary candidate.

### `validator.py`

Wraps veraPDF and parses `validationReport@isCompliant`. A process exit code is not treated as a conformance oracle. The requested PDF/A flavour is passed explicitly.

### `fidelity.py`

Provides a second, independent preservation oracle. Source and candidate are rendered through the same Ghostscript raster pipeline, then compared page-by-page with Pillow. The report records page counts, dimensions, mean raster error, changed-pixel ratio and per-page pass/fail state.

`warn` mode reports drift without changing publication semantics. `strict` mode turns visual drift into a publication failure. Fidelity is not a substitute for veraPDF and does not prove text/link/annotation semantics.

### `security.py`

Handles regular-file checks, optional input-size limits, password failures and in-process decryption. Passwords are never forwarded to Ghostscript.

### `orchestrator.py`

Owns the state machine:

1. validate the input path;
2. preflight the source;
3. reject signed PDFs unless signature invalidation was explicitly allowed;
4. preserve an already-valid PDF unchanged when veraPDF confirms it;
5. decrypt encrypted input to a private temporary file if necessary;
6. select a backend;
7. use Ghostscript as an automatic fallback when the pikepdf fast path fails or veraPDF rejects its candidate;
8. run optional fidelity checking against the final candidate;
9. publish with an atomic replace only after every requested gate passes.

## Backend selection

`backend="auto"` is the intended default.

The full rewrite path is selected for features that cannot be safely repaired in-place. Complex Type0/CID fonts also route away from the simple-font embedding code.

`backend="pikepdf"` is an expert override. If preflight proves a full rewrite is required, the override fails rather than silently degrading the document.

`backend="ghostscript"` forces a full rewrite and fails clearly if Ghostscript is unavailable.

## Validation and fidelity modes

`validate=False` means the library produced a PDF/A *candidate*. The CLI deliberately reports this as `UNVERIFIED`.

`validate=True` means the requested veraPDF flavour is a publication gate. The output path is not updated if validation fails.

`fidelity="off"` disables visual comparison. `warn` records a report. `strict` blocks publication when the rendered result exceeds configured tolerances or the fidelity stack is unavailable.

## Atomicity

All candidate files live in a temporary directory on the same filesystem as the destination. The final step uses `os.replace`, so a pre-existing output survives backend, validation or strict-fidelity failure and a successful candidate becomes visible atomically.

## Design constraints

- Never infer glyph mappings for complex fonts.
- Never equate an XMP PDF/A claim with conformance.
- Never expose PDF passwords to subprocess command lines.
- Never overwrite a destination when a requested validation/fidelity gate has failed.
- Never silently invalidate an existing digital signature.
- Prefer a clear failure over a plausible-looking but semantically damaged PDF.
