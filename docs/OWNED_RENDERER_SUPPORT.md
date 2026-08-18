# Owned renderer support matrix

This file describes the capabilities that are actually reachable through
`FullOwnedPageRenderer`, which is the renderer used by visual fidelity and
PDF/A-1 transparency flattening. A component module is not considered shipped
renderer support until it is composed into that canonical class and covered by
an end-to-end regression.

## Reachable today

- vector fills, clipping and affine strokes;
- TrueType/CIDFontType2 text and Type3 graphical CharProcs within the documented limits;
- embedded CFF1 `/Type1C` simple fonts using owned WinAnsi/Differences mapping;
- embedded CID-keyed `/CIDFontType0C` through Identity-H or explicit owned CMap streams;
- CFF text fill/stroke/clip render modes through the same owned text-state machine;
- raw/general-filtered images, baseline JPEG and CCITT Group 3/4/MR fax images;
- image masks and soft masks;
- device/calibrated/ICC/Indexed/Separation/DeviceN color paths already supported by the color engine;
- blend modes and isolated transparency groups;
- axial/radial ShadingType 2/3;
- Gouraud mesh ShadingType 4/5, including filtered mesh streams;
- PatternType 2 shading fills routed through the same canonical shading dispatcher;
- PatternType 1 / PaintType 1 colored tiling fills;
- owned PDF/A-1 page transparency flattening and visual fidelity.

## Explicit fail-closed renderer gaps

- ShadingType 1 function-based surfaces;
- ShadingType 6/7 Coons/tensor patch meshes;
- PatternType 1 / PaintType 2 uncolored tiling;
- pattern-colored strokes;
- CFF CID per-FD FontMatrix composition;
- Type0 vertical metrics/text;
- Type1 PFA/PFB outline rendering;
- predefined non-Identity CMaps not yet bundled by the owned CMap layer;
- JPX/JPEG 2000 and JBIG2 image codecs;
- non-isolated and knockout transparency groups;
- annotation appearance transparency flattening;
- Type3 stroke/clip text render modes and nested text where not already supported.

The production rule is fail-closed: a conversion that requires one of these
paths must not silently substitute another font, codec, rasterizer or external
engine.
