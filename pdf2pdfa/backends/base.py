"""Backend contracts and shared result types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BackendUnavailableError(RuntimeError):
    pass


class ConversionBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackendResult:
    backend: str
    output_path: Path
    stdout: str = ""
    stderr: str = ""


class ConversionBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        level: str,
        icc_profile: str | Path | None = None,
        font_path: str | Path | None = None,
    ) -> BackendResult: ...
