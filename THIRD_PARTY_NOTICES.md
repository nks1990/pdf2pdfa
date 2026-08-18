# Third-party notices

`pdf2pdfa` is MIT-licensed. The following bundled data assets have separate permissive/public-domain terms.

## Compact ICC Profiles

The bundled files `pdf2pdfa/data/sRGB.icc.b64` and `pdf2pdfa/data/CMYK.icc.b64` are Base64 encodings of:

- `sRGB-v2-micro.icc`
- `CGATS001Compat-v2-micro.icc`

from **Compact ICC Profiles** by Ethan Lee / `saucecontrol` contributors.

Upstream project: `saucecontrol/Compact-ICC-Profiles` on GitHub.

The upstream project releases all profiles in the collection to the public domain under **Creative Commons CC0 1.0 Universal**. These profiles are bundled to provide small, redistributable default RGB and CMYK color-space profiles without importing a restrictive profile license into the Python package.

The CMYK profile contains an `A2B0` mapping based on CGATS TR 001-1995 characterization data and is intended as a compact default source/display mapping. It is not presented as a press-specific output characterization profile. A caller that requires a specific printing condition should supply an appropriate ICC OutputIntent explicitly.
