"""Stable machine-readable protocol shared by headless integrations.

The protocol is intentionally stdlib/owned-only so CLI, future MCP/HTTP wrappers
and Python agents can share one versioned contract without runtime dependencies.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .native.document import PDFParseError
from .native.pipeline import (
    InputLimitError,
    OwnedFidelityError,
    OwnedPipelineError,
    OwnedValidationError,
    SignatureInvalidationError,
)
from .native.repair import UnsupportedNativeRepairError
from .native.security import (
    InvalidPasswordError,
    PDFSecurityError,
    UnsupportedSecurityHandlerError,
)


MACHINE_SCHEMA_VERSION = "1"


def envelope(
    command: str | None,
    *,
    ok: bool,
    status: str,
    exit_code: int,
    result: Any | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one top-level machine response.

    ``ok`` means the requested outcome was achieved, not merely that the
    process executed. For example, a successful validation of a non-compliant
    file returns ``ok=false`` with ``status=invalid`` and a structured result.
    """
    payload: dict[str, Any] = {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "pdf2pdfa_version": __version__,
        "ok": bool(ok),
        "status": status,
        "exit_code": int(exit_code),
        "command": command,
    }
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return payload


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Map public/runtime exceptions to stable agent-facing error codes."""
    code = "INTERNAL_ERROR"
    category = "operational_error"
    retryable = False

    if isinstance(exc, FileNotFoundError):
        code, category = "INPUT_NOT_FOUND", "invalid_input"
    elif isinstance(exc, InputLimitError):
        code, category = "INPUT_LIMIT_EXCEEDED", "invalid_input"
    elif isinstance(exc, InvalidPasswordError):
        code, category, retryable = "INVALID_PASSWORD", "invalid_input", True
    elif isinstance(exc, UnsupportedSecurityHandlerError):
        code, category = "UNSUPPORTED_SECURITY_HANDLER", "blocked"
    elif isinstance(exc, SignatureInvalidationError):
        code, category = "SIGNATURE_INVALIDATION_BLOCKED", "blocked"
    elif isinstance(exc, UnsupportedNativeRepairError):
        code, category = "UNSUPPORTED_REPAIR", "blocked"
    elif isinstance(exc, OwnedFidelityError):
        code, category = "FIDELITY_REJECTED", "blocked"
    elif isinstance(exc, OwnedValidationError):
        code, category = "OWNED_VALIDATION_FAILED", "operational_error"
    elif isinstance(exc, PDFParseError):
        code, category = "INVALID_PDF", "invalid_input"
    elif isinstance(exc, PDFSecurityError):
        code, category = "INVALID_SECURITY_STRUCTURE", "invalid_input"
    elif isinstance(exc, ValueError):
        code, category = "INVALID_ARGUMENT", "invalid_input"
    elif isinstance(exc, OSError):
        code, category, retryable = "IO_ERROR", "operational_error", True
    elif isinstance(exc, OwnedPipelineError):
        if str(exc).startswith("input is not a regular file:"):
            code, category = "INPUT_NOT_FOUND", "invalid_input"
        else:
            code, category = "PIPELINE_ERROR", "operational_error"
    elif isinstance(exc, KeyboardInterrupt):
        code, category = "INTERRUPTED", "interrupted"

    return {
        "code": code,
        "type": type(exc).__name__,
        "category": category,
        "message": str(exc),
        "retryable": retryable,
    }


__all__ = ["MACHINE_SCHEMA_VERSION", "envelope", "error_payload"]
