"""PDF/A profile policy definitions used by preflight and conversion planning."""

from __future__ import annotations

from dataclasses import dataclass


_VALID_LEVELS = {"1b", "2b", "3b"}


@dataclass(frozen=True, slots=True)
class PDFAPolicy:
    level: str
    allow_transparency: bool
    allow_embedded_files: bool
    allow_javascript: bool = False
    allow_encryption: bool = False

    @property
    def part(self) -> int:
        return int(self.level[0])

    @property
    def conformance(self) -> str:
        return self.level[1].upper()


def get_policy(level: str) -> PDFAPolicy:
    normalized = level.lower()
    if normalized not in _VALID_LEVELS:
        raise ValueError(
            f"Invalid PDF/A level '{level}'. Must be one of: {', '.join(sorted(_VALID_LEVELS))}"
        )
    if normalized == "1b":
        return PDFAPolicy(
            level=normalized,
            allow_transparency=False,
            allow_embedded_files=False,
        )
    if normalized == "2b":
        return PDFAPolicy(
            level=normalized,
            allow_transparency=True,
            allow_embedded_files=False,
        )
    return PDFAPolicy(
        level=normalized,
        allow_transparency=True,
        allow_embedded_files=True,
    )
