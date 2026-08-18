# Architecture

`pdf2pdfa` v5 is a fully owned PDF/A engine. Runtime code may use Python's standard library and code in this repository; it may not delegate PDF parsing, conversion, validation, rendering or font/image interpretation to an external engine.

## End-to-end state machine

```text
input bytes
   |
   v
owned tokenizer + PDF parser
   |
   +--> Standard Security Handler --> plaintext object graph
   |
   v
owned PDF/A validator
   |
   v
repair planner ------------------------+
   |                                   |
   | safe structural operations        | unsupported/ambiguous
   v                                   v
owned repair engine                  hard failure
   |
   +--> optional owned page renderer --> PDF/A-1 flattening
   |
   v
owned writer -> private candidate
   |
   v
reparse with owned parser
   |
   v
mandatory owned PDF/A validation
   |
   +--> semantic fidelity (structural rewrite)
   |       or
   +--> owned visual fidelity (painting rewrite)
   |
   v
atomic os.replace(destination)
```

A failure before the final `os.replace` leaves an existing destination untouched.

## Ownership boundary

`pyproject.toml` declares `dependencies = []`. `tests/owned/test_package_ownership.py` scans all distributed Python source and rejects runtime imports outside the standard library or `pdf2pdfa` itself.

Build/test tools are development tooling, not runtime dependencies.

## Core object model

### `native/objects.py`

Defines PDF names, indirect references, dictionaries and streams without binding them to a third-party object model.

### `native/tokenizer.py`

Tokenizes PDF lexical syntax: numbers, strings, hexadecimal strings, names with escapes, arrays, dictionaries, keywords and indirect references. Limits prevent pathological nesting/token sizes from becoming silent resource bombs.

### `native/document.py`

Owns parsing and object resolution. It supports classic xref tables, xref streams, object streams and incremental `/Prev` chains, with a repair-mode xref reconstruction path for malformed input. It lazily resolves indirect objects and exposes mutable/new-object operations used by repair.

### `native/writer.py`

Serializes the reachable object graph, normalizes output structure and writes deterministic classic xref output. Profile-specific code chooses PDF 1.4 for PDF/A-1 and PDF 1.7 for PDF/A-2/3.

## Streams and content

### `native/filters.py`

Owned implementations for general PDF filters and predictors, including Flate, ASCIIHex, ASCII85, RunLength and LZW.

### `native/content.py`

Parses content-stream operands/operators and inline images without relying on the document tokenizer's indirect-object grammar.

### `native/structure.py`

Walks the page tree, resolves inherited page properties/resources, decodes content streams, walks name trees and traverses reachable objects.

## PDF/A rule engine

### `native/pdfa.py`

Implements the validation policy for `1b`, `2b` and `3b`. A validation report contains:

- target level;
- passed check count;
- structured failures;
- rule id;
- clause label;
- human-readable message;
- PDF path/object location.

The validator checks file structure, encryption, stream restrictions, metadata/XMP, actions, page content, annotations, fonts, color, ICC/OutputIntent, associated/embedded files, forms, optional content and profile-specific restrictions such as PDF/A-1 transparency.

Validation is mandatory for every rewritten candidate. There is no production code path that simply writes `pdfaid:*` metadata and calls the result compliant.

## Repair planning

### `native/repair.py`

Turns validation failures into either:

- a known safe repair operation; or
- an explicit blocker.

Safe structural work includes normalization/removal of forbidden actions, compatible stream rewrites, metadata generation, color/output-intent normalization, form/annotation flags, optional content and attachment handling according to target policy.

A failure that does not have a proven repair is never guessed away.

### `native/repair_owned.py`

Extends structural repair with rendering-aware operations. Today its key role is selective PDF/A-1 transparency flattening. It maps used transparency failures to concrete pages and refuses annotation appearance cases that cannot be preserved safely.

## Fonts

### `native/ttf.py`, `native/truetype.py`

Parse SFNT/TrueType tables, cmap, metrics, embedding permissions and glyph outlines directly.

### `native/font_embed.py`

Embeds explicitly supplied font programs only when character/glyph mapping can be proven. It never searches system fonts and silently substitutes a look-alike.

### `native/cmap.py`, `native/pdf_font.py`, `native/text_render.py`

Interpret supported PDF simple and Type0/CIDFontType2 text resources, CMaps, widths and text state for owned rendering.

Unsupported CFF/Type1/Type3/predefined-CMap paths fail explicitly until the corresponding owned interpreter exists.

## Color and images

### `native/color.py`, `native/function.py`

Interpret device, calibrated, ICC, Indexed, Separation/DeviceN-related color transformations and PDF functions where supported.

### `native/icc.py`, `native/icc_transform.py`, `native/color_profiles.py`

Parse ICC structures, perform owned transforms and generate default archival profiles in source rather than bundling opaque third-party profile blobs.

### `native/jpeg.py`, `native/image.py`

Decode baseline JPEG and generic packed/filtered image data, applying Decode arrays, image masks, color-key masks, explicit masks and soft masks.

Terminal codecs that do not yet have owned decoders (currently JPX/JBIG2/CCITT in rendering paths) raise `UnsupportedImageError` rather than calling a system/image library.

## Renderer

### `native/raster.py`

Pure-Python RGBA surface, matrix/path primitives, scan conversion, clipping and compositing.

### `native/page_render.py`, `native/render.py`

Interpret supported PDF painting operators, graphics state, paths, text, images and Form XObjects. The renderer is fail-closed: an unsupported operator/resource aborts fidelity/flattening.

### `native/affine_stroke.py`

Preserves stroke geometry under nontrivial affine transforms rather than approximating line widths in device space.

### `native/transparency_render.py`

Adds opacity, blend modes, soft masks and isolated transparency groups. Non-isolated and knockout groups remain explicit unsupported branches until their backdrop semantics are implemented.

## PDF/A-1 flattening

### `native/flatten.py`

A page requiring supported transparency repair is rendered in unrotated page user space, flattened to opaque RGB and embedded as a Flate image XObject. `/Rotate`, page boxes and annotations are preserved. Old painting resources are detached so forbidden transparent resources do not remain reachable only because they were once referenced by the page.

Annotation appearance streams must be resource-independent before page-resource detachment; otherwise flattening fails.

## Fidelity

### `native/fidelity.py`

Semantic fingerprinting compares page boxes/rotation, decoded content, text-show operations, image data and attachments when a repair should not alter page painting.

### `native/visual_fidelity.py`

Source and candidate are rendered through the same owned renderer into RGB bytes in memory. Page count/geometry and pixel differences are compared with explicit tolerances. No PNG encoder, Pillow or external rasterizer is involved.

### `native/pipeline.py`

`OwnedPDFAPipeline` selects the fidelity mode:

- `auto`: semantic for structural changes, visual for intentional page-painting rewrites;
- `semantic`: forbid intentional page-painting rewrite;
- `visual`: always use owned rendering;
- `off`: skip fidelity, never validation.

## Security

### `native/aes.py`, `native/security.py`

Implement the PDF Standard Security Handler in owned code for the supported revisions, including password authentication, object encryption/decryption and AES primitives needed by PDF security.

Passwords remain in-process. No command line/subprocess boundary exists.

Applied signatures are detected before rewriting and refused by default because any rewrite invalidates signed byte ranges.

## Public API

`converter.py` is a small facade over `OwnedPDFAPipeline`. It exposes `inspect`, `validate` and `convert`.

`cli.py` uses `argparse` from the standard library. Backend/validator executable options do not exist.

## Design invariants

1. Validation is mandatory after every rewrite.
2. No candidate is published when validation fails.
3. No unimplemented transformation is delegated to an external executable/library.
4. A page-painting rewrite requires visual fidelity unless explicitly disabled.
5. A structural rewrite requires semantic fidelity in default `auto` mode.
6. Signed input is never rewritten implicitly.
7. A password never leaves the process.
8. Existing conforming unencrypted input is preserved byte-for-byte.
9. The final destination update is atomic.
10. Unsupported is an acceptable result; silent semantic damage is not.
