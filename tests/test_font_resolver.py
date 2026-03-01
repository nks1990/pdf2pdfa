"""Tests for font_resolver module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf2pdfa.font_resolver import (
    _PDF_STANDARD_14,
    _SYSTEM_FONTS,
    parse_font_name,
    resolve_font,
)


# ── parse_font_name ──────────────────────────────────────────────────

class TestParseStandard14:
    """All 14 standard PDF fonts should parse correctly."""

    @pytest.mark.parametrize(
        "name, expected",
        list(_PDF_STANDARD_14.items()),
        ids=list(_PDF_STANDARD_14.keys()),
    )
    def test_standard_14(self, name, expected):
        assert parse_font_name(name) == expected

    def test_with_leading_slash(self):
        assert parse_font_name("/Helvetica-Bold") == ("sans", "bold", "roman")

    def test_with_slash_times_roman(self):
        assert parse_font_name("/Times-Roman") == ("serif", "normal", "roman")


class TestParseSubsetPrefix:
    def test_subset_prefix_stripped(self):
        assert parse_font_name("ABCDEF+Helvetica-Bold") == ("sans", "bold", "roman")

    def test_subset_prefix_with_slash(self):
        assert parse_font_name("/XYZABC+Courier-Oblique") == ("mono", "normal", "italic")

    def test_subset_prefix_times_bolditalic(self):
        assert parse_font_name("AAAAAA+Times-BoldItalic") == ("serif", "bold", "italic")


class TestParseUnknown:
    def test_unknown_defaults_to_sans(self):
        assert parse_font_name("SomeRandomFont") == ("sans", "normal", "roman")

    def test_unknown_bold_detected(self):
        assert parse_font_name("SomeRandomFont-Bold") == ("sans", "bold", "roman")

    def test_unknown_italic_detected(self):
        assert parse_font_name("SomeRandomFont-Italic") == ("sans", "normal", "italic")

    def test_unknown_boldoblique_detected(self):
        assert parse_font_name("WeirdFont-BoldOblique") == ("sans", "bold", "italic")


class TestParseAliases:
    def test_arial_is_sans(self):
        assert parse_font_name("Arial") == ("sans", "normal", "roman")

    def test_arial_bold(self):
        assert parse_font_name("Arial-Bold") == ("sans", "bold", "roman")

    def test_timesnewroman_is_serif(self):
        cat, _, _ = parse_font_name("TimesNewRoman")
        assert cat == "serif"

    def test_couriernew_is_mono(self):
        cat, _, _ = parse_font_name("CourierNew")
        assert cat == "mono"


# ── resolve_font ─────────────────────────────────────────────────────

class TestResolveUserOverride:
    def test_user_override_returned(self, tmp_path):
        font_file = tmp_path / "custom.ttf"
        font_file.write_bytes(b"fake")
        result = resolve_font("/Helvetica", str(font_file))
        assert result == str(font_file)

    def test_user_override_missing_falls_through(self):
        result = resolve_font("/Helvetica", "/nonexistent/path.ttf")
        # Falls through to system resolution (may or may not find a font)
        assert result != "/nonexistent/path.ttf"


class TestResolveSystemFonts:
    """Test system font resolution on the current platform."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_system_fonts(self):
        """Skip if running in CI without system fonts."""
        result = resolve_font("/Helvetica")
        if result is None:
            pytest.skip("No system fonts available")

    def test_resolve_helvetica_regular(self):
        result = resolve_font("/Helvetica")
        assert result is not None
        assert Path(result).is_file()

    def test_resolve_times_bold(self):
        result = resolve_font("/Times-Bold")
        assert result is not None
        assert Path(result).is_file()

    def test_resolve_courier_oblique(self):
        result = resolve_font("/Courier-Oblique")
        assert result is not None
        assert Path(result).is_file()

    def test_resolved_fonts_differ_by_category(self):
        sans = resolve_font("/Helvetica")
        serif = resolve_font("/Times-Roman")
        mono = resolve_font("/Courier")
        # At least sans and serif should differ (mono may degrade to sans on some systems)
        if sans and serif:
            assert sans != serif


class TestResolveDegradation:
    """Test that degradation works when exact match is missing."""

    def test_missing_bold_degrades_to_normal(self, tmp_path):
        normal_font = tmp_path / "normal.ttf"
        normal_font.write_bytes(b"fake")

        fake_fonts = {
            ("sans", "normal", "roman"): str(normal_font),
        }

        with patch("pdf2pdfa.font_resolver._current_platform", return_value="win32"), \
             patch.dict(_SYSTEM_FONTS, {"win32": fake_fonts}):
            result = resolve_font("/Helvetica-Bold")
            assert result == str(normal_font)

    def test_missing_category_degrades_to_sans(self, tmp_path):
        sans_font = tmp_path / "sans.ttf"
        sans_font.write_bytes(b"fake")

        fake_fonts = {
            ("sans", "normal", "roman"): str(sans_font),
        }

        with patch("pdf2pdfa.font_resolver._current_platform", return_value="win32"), \
             patch.dict(_SYSTEM_FONTS, {"win32": fake_fonts}):
            result = resolve_font("/Times-Roman")
            assert result == str(sans_font)

    def test_nothing_available_returns_none(self):
        with patch("pdf2pdfa.font_resolver._current_platform", return_value="win32"), \
             patch.dict(_SYSTEM_FONTS, {"win32": {}}):
            result = resolve_font("/Helvetica")
            assert result is None
