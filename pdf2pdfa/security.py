"""Security helpers for handling untrusted and encrypted PDF inputs."""

from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Pdf
from pikepdf.exceptions import PasswordError


class InputSecurityError(RuntimeError):
    pass


class PasswordRequiredError(InputSecurityError):
    pass


def validate_input_file(path: str | Path, *, max_bytes: int | None = None) -> Path:
    """Validate basic filesystem properties before parsing an untrusted PDF."""
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise InputSecurityError(f"Input is not a regular file: {candidate}")
    size = candidate.stat().st_size
    if size <= 0:
        raise InputSecurityError(f"Input PDF is empty: {candidate}")
    if max_bytes is not None and size > max_bytes:
        raise InputSecurityError(
            f"Input PDF is {size} bytes, exceeding the configured limit of {max_bytes} bytes"
        )
    return candidate


def open_pdf(path: str | Path, *, password: str | bytes | None = None) -> Pdf:
    """Open a PDF while translating password failures into a stable API error."""
    try:
        return Pdf.open(str(path), password=password or "")
    except PasswordError as exc:
        raise PasswordRequiredError(
            "The PDF is encrypted and the supplied password is missing or incorrect"
        ) from exc


def decrypt_to_file(
    input_path: str | Path,
    output_path: str | Path,
    *,
    password: str | bytes | None,
) -> Path:
    """Write an unencrypted private working copy without exposing the password to subprocesses."""
    output = Path(output_path)
    with open_pdf(input_path, password=password) as pdf:
        if not bool(getattr(pdf, "is_encrypted", False)):
            raise InputSecurityError("decrypt_to_file called for an unencrypted PDF")
        pdf.save(str(output), encryption=False)
    return output


def read_password_file(path: str | Path) -> str:
    """Read one password from a file, removing only a final line ending."""
    value = Path(path).read_text(encoding="utf-8")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n") or value.endswith("\r"):
        value = value[:-1]
    return value
