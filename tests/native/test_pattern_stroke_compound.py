from __future__ import annotations

from pdf2pdfa.native.owned_renderer import render_page_full

from tests.native.test_pattern_stroke import _pdf, _pixel, _white
from tests.native.test_tiling_pattern_render import _colored_cell


def test_compound_tiling_fill_and_tiling_stroke_reuse_same_original_path():
    page = render_page_full(
        _pdf(
            _colored_cell(),
            b"/Pattern cs /P scn "
            b"/Pattern CS /P SCN 8 w "
            b"10 10 80 20 re B\n",
        ),
        dpi=72,
    )

    # Interior proves the tiling fill ran. A pixel outside the rectangle but
    # inside the centered stroke proves that the fill painter did not consume
    # the path before the stroke phase.
    assert not _white(_pixel(page, 20, 20))
    assert not _white(_pixel(page, 8, 20))
    assert _white(_pixel(page, 3, 20))
