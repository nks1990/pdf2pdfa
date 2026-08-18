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


_COMPONENTS = {
    "GRAY": 1,
    "RGB ": 3,
    "CMYK": 4,
    "XYZ ": 3,
    "Lab ": 3,
}


def _decode_profile_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    # Bundled assets are text/base64 so wheels remain easy to inspect.  A real
    # binary ICC supplied by a user must pass through unchanged.
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return data
    if text and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r" for ch in text):
        try:
            decoded = base64.b64decode(text, validate=False)
            if len(decoded) >= 128:
                return decoded
        except Exception:
            pass
    return data


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
    return ICCProfileInfo(
        path=str(path),
        data=data,
        color_space=color_space,
        components=components,
        profile_class=profile_class,
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
