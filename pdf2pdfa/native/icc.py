"""Pure-Python ICC profile parsing used by native PDF/A validation and repair."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ICCError(ValueError):
    pass


_COLOR_COMPONENTS = {
    "GRAY": 1,
    "RGB ": 3,
    "CMYK": 4,
    "XYZ ": 3,
    "Lab ": 3,
}


@dataclass(frozen=True, slots=True)
class ICCTag:
    signature: str
    offset: int
    size: int


@dataclass(frozen=True, slots=True)
class ICCProfile:
    data: bytes
    declared_size: int
    version_major: int
    profile_class: str
    color_space: str
    pcs: str
    tags: Mapping[str, ICCTag]

    @property
    def components(self) -> int:
        if self.color_space in _COLOR_COMPONENTS:
            return _COLOR_COMPONENTS[self.color_space]
        if len(self.color_space) == 4 and self.color_space[1:] == "CLR" and self.color_space[0].isdigit():
            return int(self.color_space[0])
        if len(self.color_space) == 4 and self.color_space[2:] == "CL" and self.color_space[:2].isdigit():
            return int(self.color_space[:2])
        raise ICCError(f"unsupported ICC color space {self.color_space!r}")

    @property
    def has_device_to_pcs(self) -> bool:
        if any(tag in self.tags for tag in ("A2B0", "mAB ")):
            return True
        if self.color_space == "RGB ":
            return all(tag in self.tags for tag in ("rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC"))
        if self.color_space == "GRAY":
            return "kTRC" in self.tags or "A2B0" in self.tags
        return False

    @property
    def has_pcs_to_device(self) -> bool:
        if any(tag in self.tags for tag in ("B2A0", "mBA ")):
            return True
        if self.color_space == "RGB ":
            return all(tag in self.tags for tag in ("rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC"))
        if self.color_space == "GRAY":
            return "kTRC" in self.tags
        return False

    def tag_data(self, signature: str) -> bytes:
        tag = self.tags[signature]
        return self.data[tag.offset : tag.offset + tag.size]


def parse_icc(data: bytes) -> ICCProfile:
    if len(data) < 132:
        raise ICCError("ICC profile is shorter than header + tag-count field")
    declared_size = int.from_bytes(data[0:4], "big")
    if declared_size < 132:
        raise ICCError(f"invalid ICC declared size {declared_size}")
    if declared_size > len(data):
        raise ICCError(
            f"ICC declares {declared_size} bytes but only {len(data)} are embedded"
        )
    data = data[:declared_size]
    if data[36:40] != b"acsp":
        raise ICCError("ICC profile is missing the acsp signature")
    version_major = data[8]
    if version_major not in (2, 4):
        raise ICCError(f"unsupported ICC major version {version_major}")
    profile_class = data[12:16].decode("ascii", "replace")
    color_space = data[16:20].decode("ascii", "replace")
    pcs = data[20:24].decode("ascii", "replace")
    if pcs not in ("XYZ ", "Lab "):
        raise ICCError(f"invalid ICC PCS {pcs!r}")
    tag_count = int.from_bytes(data[128:132], "big")
    table_end = 132 + 12 * tag_count
    if tag_count > 4096 or table_end > len(data):
        raise ICCError("ICC tag table is truncated or unreasonably large")
    tags: dict[str, ICCTag] = {}
    for index in range(tag_count):
        base = 132 + index * 12
        signature = data[base : base + 4].decode("ascii", "replace")
        offset = int.from_bytes(data[base + 4 : base + 8], "big")
        size = int.from_bytes(data[base + 8 : base + 12], "big")
        if size <= 0:
            raise ICCError(f"ICC tag {signature!r} has zero size")
        if offset < table_end or offset + size > len(data):
            raise ICCError(f"ICC tag {signature!r} points outside profile data")
        tags[signature] = ICCTag(signature, offset, size)
    profile = ICCProfile(
        data=data,
        declared_size=declared_size,
        version_major=version_major,
        profile_class=profile_class,
        color_space=color_space,
        pcs=pcs,
        tags=tags,
    )
    _ = profile.components
    return profile


def load_icc(path: str | Path) -> ICCProfile:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return parse_icc(raw)
    compact = "".join(text.split())
    if compact and all(ch.isalnum() or ch in "+/=" for ch in compact):
        try:
            decoded = base64.b64decode(compact, validate=True)
            if len(decoded) >= 132:
                return parse_icc(decoded)
        except (ValueError, binascii.Error):
            pass
    return parse_icc(raw)
