from __future__ import annotations

from pdf2pdfa.native.owned_renderer import FullOwnedPageRenderer
from pdf2pdfa.native.type1_text_render import FullOutlineTextRenderer


def test_full_renderer_uses_single_outline_text_machine_for_type1_cff_and_truetype():
    # The production MRO shall keep the Type1 bridge before the CFF bridge so
    # its `_ensure_outline_text_renderer` factory upgrades the shared renderer
    # rather than creating a parallel text-state implementation.
    names = [cls.__name__ for cls in FullOwnedPageRenderer.__mro__]
    assert names.index("Type1TextPageRendererMixin") < names.index("CFFTextPageRendererMixin")
    assert issubclass(FullOutlineTextRenderer, object)
