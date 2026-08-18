"""Structured models shared by preflight, conversion and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    code: str
    message: str
    severity: Severity
    repairable: bool = False
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PreflightReport:
    level: str
    features: dict[str, Any] = field(default_factory=dict)
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def errors(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.WARNING]

    def add(
        self,
        code: str,
        message: str,
        severity: Severity,
        *,
        repairable: bool = False,
        **context: Any,
    ) -> None:
        self.issues.append(
            PreflightIssue(
                code=code,
                message=message,
                severity=severity,
                repairable=repairable,
                context=context,
            )
        )
