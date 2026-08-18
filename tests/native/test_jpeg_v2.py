from __future__ import annotations

from pdf2pdfa.native.jpeg import UnsupportedJPEGError, decode_jpeg


def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def minimal_gray_jpeg() -> bytes:
    dqt = bytes([0]) + bytes([1] * 64)
    counts = bytes([1] + [0] * 15)
    dht = bytes([0x00]) + counts + b"\x00"
    dht += bytes([0x10]) + counts + b"\x00"
    sof = b"\x08" + (8).to_bytes(2, "big") + (8).to_bytes(2, "big")
    sof += bytes([1, 1, 0x11, 0])
    sos = bytes([1, 1, 0, 0, 63, 0])
    entropy = bytes([0b00111111])
    return (
        b"\xff\xd8"
        + _segment(0xDB, dqt)
        + _segment(0xC4, dht)
        + _segment(0xC0, sof)
        + _segment(0xDA, sos)
        + entropy
        + b"\xff\xd9"
    )


def test_hand_built_gray_baseline_jpeg():
    image = decode_jpeg(minimal_gray_jpeg())
    assert (image.width, image.height, image.mode) == (8, 8, "L")
    assert image.pixels == bytes([128] * 64)


def test_progressive_jpeg_is_rejected_explicitly():
    data = minimal_gray_jpeg().replace(b"\xff\xc0", b"\xff\xc2", 1)
    try:
        decode_jpeg(data)
    except UnsupportedJPEGError as exc:
        assert "progressive" in str(exc).lower()
    else:
        raise AssertionError("progressive JPEG must not be silently mis-decoded")
