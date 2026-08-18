from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.function import PDFFunction, UnsupportedFunctionError
from pdf2pdfa.native.objects import PDFDict, PDFName, PDFStream


def _single(value):
    builder = PDFBuilder(version="1.7")
    ref = builder.add(value)
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    return PDFFunction(doc, ref)


def test_exponential_function():
    function = _single(PDFDict({
        "FunctionType": 2, "Domain": [0, 1], "Range": [0, 1, 0, 1],
        "C0": [0, 1], "C1": [1, 0], "N": 1,
    }))
    assert function.evaluate([0.25]) == [0.25, 0.75]


def test_sampled_order1_interpolates_linearly():
    function = _single(PDFStream(PDFDict({
        "FunctionType": 0, "Domain": [0, 1], "Range": [0, 1],
        "Size": [2], "BitsPerSample": 8,
    }), bytes([0, 255])))
    assert abs(function.evaluate([0.25])[0] - 0.25) < 1e-12
    assert abs(function.evaluate([0.75])[0] - 0.75) < 1e-12


def test_sampled_order3_fails_closed():
    try:
        _single(PDFStream(PDFDict({
            "FunctionType": 0, "Domain": [0, 1], "Range": [0, 1],
            "Size": [4], "BitsPerSample": 8, "Order": 3,
        }), bytes([0, 85, 170, 255])))
    except UnsupportedFunctionError as exc:
        assert "Order 3" in str(exc)
    else:
        raise AssertionError("Order 3 must not use an approximate spline")


def test_stitching_function_selects_and_encodes_segments():
    builder = PDFBuilder(version="1.7")
    f0 = builder.add(PDFDict({
        "FunctionType": 2, "Domain": [0, 1], "Range": [0, 1],
        "C0": [0], "C1": [0.5], "N": 1,
    }))
    f1 = builder.add(PDFDict({
        "FunctionType": 2, "Domain": [0, 1], "Range": [0, 1],
        "C0": [0.5], "C1": [1], "N": 1,
    }))
    stitched = builder.add(PDFDict({
        "FunctionType": 3, "Domain": [0, 1], "Range": [0, 1],
        "Functions": [f0, f1], "Bounds": [0.5], "Encode": [0, 1, 0, 1],
    }))
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    function = PDFFunction(doc, stitched)
    assert abs(function.evaluate([0.25])[0] - 0.25) < 1e-12
    assert abs(function.evaluate([0.75])[0] - 0.75) < 1e-12


def test_calculator_exp_operand_order():
    function = _single(PDFStream(
        PDFDict({"FunctionType": 4, "Domain": [0, 10], "Range": [0, 100]}),
        b"{ 2 exp }",
    ))
    assert function.evaluate([3]) == [9.0]


def test_calculator_atan_operand_order():
    function = _single(PDFStream(
        PDFDict({"FunctionType": 4, "Domain": [-10,10,-10,10], "Range": [0,360]}),
        b"{ atan }",
    ))
    assert abs(function.evaluate([1, 1])[0] - 45.0) < 1e-12
    assert abs(function.evaluate([1, -1])[0] - 135.0) < 1e-12


def test_calculator_not_and_ifelse():
    function = _single(PDFStream(
        PDFDict({"FunctionType": 4, "Domain": [0,1], "Range": [0,1]}),
        b"{ 0.5 gt not { 0 } { 1 } ifelse }",
    ))
    assert function.evaluate([0.25]) == [0.0]
    assert function.evaluate([0.75]) == [1.0]
