"""ICC profile loading, validation and OutputIntent embedding."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
from pathlib import Path

from pikepdf import Array, Dictionary, Name, Pdf, Stream, String

logger = logging.getLogger(__name__)


class InvalidICCProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ICCProfileInfo:
    path: str
    data: bytes
    color_space: str
    components: int
    profile_class: str
    tags: frozenset[str]


_COMPONENTS = {
    "GRAY": 1,
    "RGB ": 3,
    "CMYK": 4,
    "XYZ ": 3,
    "Lab ": 3,
}


def _decode_profile_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return data
    if text and all(
        ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r"
        for ch in text
    ):
        try:
            decoded = base64.b64decode(text, validate=False)
            if len(decoded) >= 128:
                return decoded
        except Exception:
            pass
    return data


def _parse_tag_table(data: bytes, path: Path) -> frozenset[str]:
    if len(data) < 132:
        raise InvalidICCProfileError(f"ICC profile has no tag table: {path}")
    count = int.from_bytes(data[128:132], "big", signed=False)
    if count > 4096:
        raise InvalidICCProfileError(f"ICC profile has unreasonable tag count {count}: {path}")
    table_end = 132 + count * 12
    if table_end > len(data):
        raise InvalidICCProfileError(f"ICC tag table exceeds profile length: {path}")

    tags: set[str] = set()
    for index in range(count):
        pos = 132 + index * 12
        signature_bytes = data[pos : pos + 4]
        try:
            signature = signature_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InvalidICCProfileError(
                f"ICC tag {index} has a non-ASCII signature: {path}"
            ) from exc
        offset = int.from_bytes(data[pos + 4 : pos + 8], "big", signed=False)
        size = int.from_bytes(data[pos + 8 : pos + 12], "big", signed=False)
        if size <= 0 or offset < 128 or offset + size > len(data):
            raise InvalidICCProfileError(
                f"ICC tag {signature!r} points outside profile data: {path}"
            )
        tags.add(signature)
    return frozenset(tags)


def _validate_device_mapping(
    *,
    color_space: str,
    tags: frozenset[str],
    path: Path,
) -> None:
    a2b = {"A2B0", "A2B1", "A2B2"}
    if color_space == "CMYK" and not tags.intersection(a2b):
        raise InvalidICCProfileError(
            f"CMYK ICC profile lacks a device-to-PCS A2B mapping: {path}"
        )
    if color_space == "RGB ":
        matrix_profile = {
            "rXYZ",
            "gXYZ",
            "bXYZ",
            "rTRC",
            "gTRC",
            "bTRC",
        }.issubset(tags)
        if not matrix_profile and not tags.intersection(a2b):
            raise InvalidICCProfileError(
                f"RGB ICC profile lacks matrix/TRC or A2B device mapping tags: {path}"
            )
    if color_space == "GRAY" and "kTRC" not in tags and not tags.intersection(a2b):
        raise InvalidICCProfileError(
            f"Gray ICC profile lacks kTRC or A2B device mapping tags: {path}"
        )


def read_icc_profile(icc_path: str | Path) -> ICCProfileInfo:
    path = Path(icc_path)
    if not path.is_file():
        raise FileNotFoundError(f"ICC profile not found: {path}")
    data = _decode_profile_bytes(path)
    if len(data) < 128:
        raise InvalidICCProfileError(f"ICC profile is too short: {path}")

    declared_size = int.from_bytes(data[0:4], "big", signed=False)
    if declared_size and declared_size > len(data):
        raise InvalidICCProfileError(
            f"ICC profile declares {declared_size} bytes but only {len(data)} are available: {path}"
        )
    if data[36:40] != b"acsp":
        raise InvalidICCProfileError(f"Missing ICC 'acsp' signature: {path}")

    profile_class = data[12:16].decode("ascii", "replace")
    color_space = data[16:20].decode("ascii", "replace")
    components = _COMPONENTS.get(color_space)
    if components is None:
        raise InvalidICCProfileError(
            f"Unsupported ICC data color space {color_space!r}: {path}"
        )
    if profile_class in {"link", "nmcl"}:
        raise InvalidICCProfileError(
            f"ICC profile class {profile_class!r} is not valid for an ICCBased/OutputIntent profile: {path}"
        )

    tags = _parse_tag_table(data, path)
    _validate_device_mapping(color_space=color_space, tags=tags, path=path)
    return ICCProfileInfo(
        path=str(path),
        data=data,
        color_space=color_space,
        components=components,
        profile_class=profile_class,
        tags=tags,
    )


def make_icc_stream(pdf: Pdf, profile: ICCProfileInfo) -> Stream:
    stream = pdf.make_stream(profile.data)
    stream.stream_dict["/N"] = profile.components
    return stream


def embed_icc_profile(
    pdf: Pdf,
    icc_path: str | Path,
    *,
    output_condition_identifier: str | None = None,
) -> Stream:
    """Embed a validated ICC profile as the document PDF/A OutputIntent."""
    profile = read_icc_profile(icc_path)
    stream = make_icc_stream(pdf, profile)
    identifier = output_condition_identifier or Path(profile.path).stem

    intent = Dictionary(
        {
            "/Type": Name("/OutputIntent"),
            "/S": Name("/GTS_PDFA1"),
            "/RegistryName": String("http://www.color.org"),
            "/OutputConditionIdentifier": String(identifier),
            "/Info": String(identifier),
            "/DestOutputProfile": stream,
        }
    )
    pdf.Root.OutputIntents = Array([intent])
    logger.debug(
        "Embedded OutputIntent %s (%s, N=%d)",
        profile.path,
        profile.color_space.strip(),
        profile.components,
    )
    return stream
