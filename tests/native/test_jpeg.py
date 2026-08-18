from __future__ import annotations

from pdf2pdfa.native.jpeg import JPEGDecoder, UnsupportedJPEGError, decode_jpeg


def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def minimal_gray_jpeg() -> bytes:
    # Quantization table of ones.
    dqt = bytes([0x00]) + bytes([1] * 64)
    # One-symbol DC table (category 0 => zero difference) and one-symbol AC
    # table (EOB). Both symbols use Huffman code 0 of length 1.
    counts = bytes([1] + [0] * 15)
    dht = bytes([0x00]) + counts + bytes([0x00])
    dht += bytes([0x10]) + counts + bytes([0x00])
    sof0 = bytes([8]) + (8).to_bytes(2, "big") + (8).to_bytes(2, "big")
    sof0 += bytes([1, 1, 0x11, 0])
    sos = bytes([1, 1, 0x00, 0, 63, 0])
    # DC code 0 + AC/EOB code 0, then all-one pad bits.
    entropy = bytes([0b00111111])
    return (
        b"\xff\xd8"
        + _segment(0xDB, dqt)
        + _segment(0xC4, dht)
        + _segment(0xC0, sof0)
        + _segment(0xDA, sos)
        + entropy
        + b"\xff\xd9"
    )


def test_hand_built_baseline_jpeg_decodes_to_neutral_gray():
    image = decode_jpeg(minimal_gray_jpeg())
    assert image.width == 8
    assert image.height == 8
    assert image.mode == "L"
    assert image.pixels == bytes([128] * 64)


def test_progressive_frame_is_explicitly_rejected_not_misdecoded():
    data = minimal_gray_jpeg().replace(b"\xff\xc0", b"\xff\xc2", 1)
    try:
        decode_jpeg(data)
    except UnsupportedJPEGError as exc:
        assert "progressive" in str(exc).lower()
    else:
        raise AssertionError("progressive JPEG should have been rejected")


def test_invalid_huffman_entropy_fails_closed():
    data = minimal_gray_jpeg().replace(bytes([0b00111111]), bytes([0b11111111]))
    try:
        decode_jpeg(data)
    except Exception:
        pass
    else:
        raise AssertionError("invalid entropy code unexpectedly decoded")
