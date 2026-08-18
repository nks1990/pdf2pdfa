from __future__ import annotations

from pdf2pdfa.native.builder import PDFBuilder
from pdf2pdfa.native.document import PDFDocument
from pdf2pdfa.native.function import PDFFunction, UnsupportedFunctionError
from pdf2pdfa.native.objects import PDFDict, PDFStream


def _function(value):
    builder = PDFBuilder(version="1.7")
    ref = builder.add(value)
    pages = PDFDict({"Type": __import__('pdf2pdfa.native.objects', fromlist=['PDFName']).PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": __import__('pdf2pdfa.native.objects', fromlist=['PDFName']).PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    return PDFFunction(doc, ref)


def test_type2_exponential_interpolation():
    function = _function(
        PDFDict(
            {
                "FunctionType": 2,
                "Domain": [0, 1],
                "Range": [0, 1, 0, 1],
                "C0": [0, 1],
                "C1": [1, 0],
                "N": 1,
            }
        )
    )
    assert function.evaluate([0.25]) == [0.25, 0.75]


def test_type0_linear_sampled_interpolation():
    function = _function(
        PDFStream(
            PDFDict(
                {
                    "FunctionType": 0,
                    "Domain": [0, 1],
                    "Range": [0, 1],
                    "Size": [2],
                    "BitsPerSample": 8,
                }
            ),
            bytes([0, 255]),
        )
    )
    assert abs(function.evaluate([0.25])[0] - 0.25) < 1e-12
    assert abs(function.evaluate([0.75])[0] - 0.75) < 1e-12


def test_type0_order3_fails_closed_instead_of_using_approximate_spline():
    try:
        _function(
            PDFStream(
                PDFDict(
                    {
                        "FunctionType": 0,
                        "Domain": [0, 1],
                        "Range": [0, 1],
                        "Size": [4],
                        "BitsPerSample": 8,
                        "Order": 3,
                    }
                ),
                bytes([0, 85, 170, 255]),
            )
        )
    except UnsupportedFunctionError as exc:
        assert "Order 3" in str(exc)
    else:
        raise AssertionError("Order 3 sampled function must fail closed")


def test_type3_stitches_component_functions_at_bound():
    builder = PDFBuilder(version="1.7")
    f0 = builder.add(PDFDict({"FunctionType": 2, "Domain": [0, 1], "Range": [0, 1], "C0": [0], "C1": [0.5], "N": 1}))
    f1 = builder.add(PDFDict({"FunctionType": 2, "Domain": [0, 1], "Range": [0, 1], "C0": [0.5], "C1": [1], "N": 1}))
    stitched = builder.add(
        PDFDict(
            {
                "FunctionType": 3,
                "Domain": [0, 1],
                "Range": [0, 1],
                "Functions": [f0, f1],
                "Bounds": [0.5],
                "Encode": [0, 1, 0, 1],
            }
        )
    )
    from pdf2pdfa.native.objects import PDFName
    pages = PDFDict({"Type": PDFName("Pages"), "Count": 1, "Kids": []})
    pages_ref = builder.add(pages)
    root = builder.add(PDFDict({"Type": PDFName("Catalog"), "Pages": pages_ref}))
    builder.set_root(root)
    doc = PDFDocument.open(builder.to_bytes(), repair=False)
    function = PDFFunction(doc, stitched)
    assert abs(function.evaluate([0.25])[0] - 0.25) < 1e-12
    assert abs(function.evaluate([0.75])[0] - 0.75) < 1e-12


def test_type4_exp_uses_postscript_base_exponent_order():
    function = _function(
        PDFStream(
            PDFDict({"FunctionType": 4, "Domain": [0, 10], "Range": [0, 100]}),
            b"{ 2 exp }",
        )
    )
    assert function.evaluate([3]) == [9.0]


def test_type4_atan_uses_postscript_numerator_denominator_order():
    function = _function(
        PDFStream(
            PDFDict({"FunctionType": 4, "Domain": [-10, 10, -10, 10], "Range": [0, 360]}),
            b"{ atan }",
        )
    )
    assert abs(function.evaluate([1, 1])[0] - 45.0) < 1e-12
    assert abs(function.evaluate([1, -1])[0] - 135.0) < 1e-12


def test_type4_boolean_not_and_ifelse():
    function = _function(
        PDFStream(
            PDFDict({"FunctionType": 4, "Domain": [0, 1], "Range": [0, 1]}),
            b"{ 0.5 gt not { 0 } { 1 } ifelse }",
        )
    )
    assert function.evaluate([0.25]) == [1.0]
    assert function.evaluate([0.75]) == [0.0]
