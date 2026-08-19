# Owned renderer support matrix

This file describes the capabilities that are actually reachable through
`FullOwnedPageRenderer`, which is the renderer used by visual fidelity and
PDF/A-1 transparency flattening. A component module is not considered shipped
renderer support until it is composed into that canonical class and covered by
an end-to-end regression.

## Reachable today

- vector fills, clipping and affine strokes;
- TrueType/CIDFontType2 text and Type3 graphical CharProcs within the documented limits;
- embedded Type1 PFA/PFB `/FontFile` programs rendered directly from owned eexec/CharString outlines, including PFA/PFB, local Subrs, PDF-authoritative Widths, explicit PDF encodings, built-in StandardEncoding/custom 256-array encoding fallback and Differences semantics;
- Type1 fill/stroke/clip render modes through the same owned text-state machine used by TrueType/CFF, including Type1 inside transparency groups and annotation appearances;
- embedded CFF1 `/Type1C` simple fonts using owned WinAnsi/Differences mapping;
- embedded CID-keyed `/CIDFontType0C` through Identity-H or explicit owned CMap streams;
- CFF text fill/stroke/clip render modes through the same owned text-state machine;
- raw/general-filtered images, baseline JPEG and CCITT Group 3/4/MR fax images;
- image masks and soft masks;
- device/calibrated/ICC/Indexed/Separation/DeviceN color paths already supported by the color engine;
- blend modes and isolated transparency groups in the owned RGB compositor;
- non-isolated transparency groups with implicit/DeviceRGB blending space, including translucent backdrops and boundary alpha/blend/soft-mask composition;
- function-based ShadingType 1 surfaces with two-input owned PDF Functions;
- axial/radial ShadingType 2/3;
- Gouraud mesh ShadingType 4/5, including filtered mesh streams;
- PatternType 2 shading fills routed through the same canonical shading dispatcher;
- PatternType 1 / PaintType 1 colored tiling fills;
- PatternType 1 / PaintType 2 uncolored tiling fills through an owned shape-mask renderer for paths, text, strokes and ImageMask, with base color supplied by `[/Pattern base] ... scn`;
- normal annotation `/AP /N` rendering with BBox/Matrix-to-Rect mapping, state selection and page rotation;
- annotation-aware visual fidelity;
- PDF/A-1 annotation appearance transparency repair by baking the current normal appearance into the static page raster and neutralizing AP painting streams while retaining annotation dictionary/Rect/Subtype/AS semantics;
- owned PDF/A-1 page transparency flattening and visual fidelity.

## Explicit fail-closed renderer gaps

- ShadingType 6/7 Coons/tensor patch meshes;
- pattern-colored strokes;
- PaintType 2 cell soft masks/transparency groups and intrinsic-color content;
- Type1 `seac` composites and `callothersubr`/Flex glyph programs;
- Type1 built-in encodings that require a general PostScript VM and non-ASCII MacRoman entries not yet in the owned table;
- CFF CID per-FD FontMatrix composition;
- Type0 vertical metrics/text;
- predefined non-Identity CMaps not yet bundled by the owned CMap layer;
- JPX/JPEG 2000 and JBIG2 image codecs;
- knockout transparency groups;
- transparency groups with an explicit non-RGB blending color space;
- annotation NoZoom/NoRotate/ToggleNoView display transforms;
- dynamic annotation appearance interaction after PDF/A-1 static appearance baking (R/D streams are neutralized);
- Type3 stroke/clip text render modes and nested text where not already supported.

The production rule is fail-closed: a conversion that requires one of these
paths must not silently substitute another font, codec, rasterizer or external
engine.
