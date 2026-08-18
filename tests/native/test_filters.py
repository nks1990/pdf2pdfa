from __future__ import annotations

import zlib

from pdf2pdfa.native.filters import (
    PredictorParams,
    ascii85_decode,
    ascii85_encode,
    ascii_hex_decode,
    ascii_hex_encode,
    decode_pipeline,
    decode_predictor,
    flate_encode,
    run_length_decode,
    run_length_encode,
)


def test_ascii_hex_roundtrip_and_odd_nibble():
    data = b"\x00abc\xff"
    assert ascii_hex_decode(ascii_hex_encode(data)) == data
    assert ascii_hex_decode(b"41 42 F>") == b"AB\xf0"


def test_ascii85_roundtrip():
    data = bytes(range(256))
    assert ascii85_decode(ascii85_encode(data)) == data


def test_run_length_roundtrip_mixed_runs():
    data = b"AAAAABCDDDDDDEFGHIJKLMNOPPPZZ"
    encoded = run_length_encode(data)
    assert encoded.endswith(b"\x80")
    assert run_length_decode(encoded) == data


def test_flate_pipeline():
    data = (b"native-pdf-engine\n" * 100) + bytes(range(64))
    encoded = flate_encode(data)
    assert decode_pipeline(encoded, ["FlateDecode"]) == data


def test_png_up_predictor_decode():
    # Two one-component rows, 4 columns. Predictor 12 is fixed PNG Up,
    # therefore the second encoded row stores deltas from the first.
    encoded = bytes([10, 20, 30, 40, 1, 2, 3, 4])
    decoded = decode_predictor(
        encoded,
        PredictorParams(predictor=12, colors=1, bits_per_component=8, columns=4),
    )
    assert decoded == bytes([10, 20, 30, 40, 11, 22, 33, 44])


def test_flate_with_png_up_predictor():
    encoded_rows = bytes([10, 20, 30, 40, 1, 2, 3, 4])
    compressed = zlib.compress(encoded_rows)
    decoded = decode_pipeline(
        compressed,
        ["FlateDecode"],
        [{"Predictor": 12, "Colors": 1, "BitsPerComponent": 8, "Columns": 4}],
    )
    assert decoded == bytes([10, 20, 30, 40, 11, 22, 33, 44])
