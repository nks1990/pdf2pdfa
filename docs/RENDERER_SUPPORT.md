# Owned renderer support matrix

This document describes the rendering capabilities that are actually reachable
through `FullOwnedPageRenderer`, the renderer used by visual fidelity and
PDF/A-1 transparency flattening. A parser or helper module existing elsewhere
in the repository does not count as production support unless it is reachable
through that renderer.

## Supported

- Vector fills and clipping, including even-odd rules.
- Affine strokes, dash/cap/join/hairline handling and pattern strokes.
- TrueType and CIDFontType2 outlines.
- CFF1 / Type2 CharStrings, including CID FDSelect and per-FD FontMatrix.
- Embedded Type1 PFA/PFB, StandardEncoding/Differences, `seac` and standard Flex OtherSubrs.
- Type3 CharProcs, 2D FontMatrix advance and PDF text-rendering-mode semantics.
- Type0 horizontal and vertical text state with DW2/W2 metrics.
- Device and owned ICC color transforms.
- Raw/filtered images, baseline JPEG and CCITT Group 3/4/mixed fax.
- Image masks and soft masks in ordinary transparency rendering.
- PDF blend modes implemented by the owned raster engine.
- Shading types 1-7, including Coons and tensor-product patch meshes.
- PatternType 2 shading patterns, routed through the same shading dispatcher.
- PatternType 1 colored and bounded uncolored tiling patterns.
- Isolated transparency groups.
- RGB non-isolated transparency groups.
- Normal annotation appearances and static PDF/A-1 appearance repair.

All shading types are staged as one intrinsic graphical object before outer
clip, constant alpha, soft-mask and blend state are applied. This prevents
Background+mesh from receiving transparency twice and preserves the shape /
opacity split needed by knockout rendering.

Patch-mesh tessellation is bounded by patch count, generated triangle count and
device-space curvature. Patch streams that exceed those owned limits fail
closed rather than silently reducing geometric accuracy.

## Bounded knockout support

Knockout transparency groups (`/Group << /S /Transparency /K true >>`) are
supported only where the renderer can retain object shape independently from
object opacity and where one PDF graphical object maps to one owned raster
transaction.

Currently supported inside knockout execution:

- path **fill-only or stroke-only** objects using solid colors;
- outline-based TrueType/CFF/Type1 text when `/TK true`, except fill+stroke
  render modes that require one compound glyph transaction;
- opaque images;
- directly painted owned shadings 1-7;
- isolated and non-isolated knockout group boundaries in the RGB/implicit
  group blending space.

The knockout raster path uses an immutable group backdrop plus independent
group-shape and group-alpha planes. A partially opaque object can therefore
replace an earlier sibling over its full geometric shape instead of treating
opacity as shape.

Currently fail-closed inside knockout execution:

- `/AIS true`;
- `/TK false` text;
- soft masks set inside the knockout group;
- Type3 glyphs;
- combined path fill+stroke (`B`, `B*`, `b`, `b*`);
- fill+stroke text render modes 2 and 6;
- pattern fills, pattern strokes or pattern-colored text;
- masked/transparent images whose decoded alpha no longer exposes whether the
  mask represented shape or opacity;
- ordinary Form XObjects, because their contents require one Form-object
  transaction rather than treating internal paint operations as knockout siblings;
- nested transparency groups while knockout execution is active;
- explicit non-RGB group blending color spaces.

These cases raise an owned `UnsupportedRenderingError`; they are never silently
rendered as ordinary source-over transparency.

## Remaining explicit renderer blockers

- Adobe predefined non-Identity CMap mapping data (Japan1/GB1/CNS1/Korea1 families).
- JPX / JPEG 2000 image decoding.
- JBIG2 image decoding.
- Explicit non-RGB transparency-group blending spaces.
- The advanced knockout interactions listed above.
- Annotation display geometry that depends on `NoZoom`, `NoRotate` or
  `ToggleNoView` and is not yet reproduced by the owned visual model.

## Release rule

A feature moves from fail-closed to supported only when all of the following are
true:

1. it is reachable from `FullOwnedPageRenderer`;
2. malformed and resource-exhaustion cases have explicit bounds;
3. a focused regression covers its rendering semantics;
4. transparency-sensitive work has a PDF/A-1 flatten + owned validation +
   visual-fidelity regression where applicable;
5. no external runtime dependency or subprocess is introduced.
