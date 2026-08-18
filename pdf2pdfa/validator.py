"""veraPDF integration with structured, profile-aware results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree as ET


@dataclass(frozen=True, slots=True)
class ValidationResult:
    compliant: bool
    flavour: str
    failed_checks: int = 0
    passed_checks: int = 0
    failed_rules: tuple[str, ...] = field(default_factory=tuple)
    raw_xml: str = ""


class ValidatorUnavailableError(RuntimeError):
    pass


class ValidationExecutionError(RuntimeError):
    pass


def parse_verapdf_xml(xml_text: str, flavour: str) -> ValidationResult:
    """Parse veraPDF XML without depending on a particular namespace prefix."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValidationExecutionError(f"veraPDF returned invalid XML: {exc}") from exc

    report = None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "validationReport":
            report = node
            break
    if report is None:
        raise ValidationExecutionError("veraPDF XML does not contain a validationReport")

    compliant = report.attrib.get("isCompliant", "false").lower() == "true"
    failed_checks = 0
    passed_checks = 0
    failed_rules: list[str] = []

    for node in report.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "details":
            failed_checks = int(node.attrib.get("failedChecks", node.attrib.get("failedRules", "0")) or 0)
            passed_checks = int(node.attrib.get("passedChecks", "0") or 0)
        elif local == "rule" and node.attrib.get("status", "").lower() == "failed":
            rule_id = node.attrib.get("specification") or node.attrib.get("clause") or node.attrib.get("testNumber")
            if rule_id:
                failed_rules.append(rule_id)

    return ValidationResult(
        compliant=compliant,
        flavour=flavour.lower(),
        failed_checks=failed_checks,
        passed_checks=passed_checks,
        failed_rules=tuple(dict.fromkeys(failed_rules)),
        raw_xml=xml_text,
    )


class VeraPDFValidator:
    def __init__(self, executable: str = "verapdf", timeout: int = 120) -> None:
        self.executable = executable
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.executable) is not None or Path(self.executable).is_file()

    def validate(self, path: str | Path, flavour: str) -> ValidationResult:
        if not self.available():
            raise ValidatorUnavailableError(
                "veraPDF is required for strict PDF/A validation but was not found on PATH"
            )
        cmd = [self.executable, "--format", "xml", "--flavour", flavour.lower(), str(path)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationExecutionError(
                f"veraPDF validation timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise ValidationExecutionError(f"Could not execute veraPDF: {exc}") from exc

        if not result.stdout.strip():
            message = result.stderr.strip() or f"veraPDF exited with status {result.returncode}"
            raise ValidationExecutionError(message)

        parsed = parse_verapdf_xml(result.stdout, flavour)
        # veraPDF exit codes are useful for execution failures, but conformance is
        # determined from validationReport@isCompliant rather than returncode.
        if result.returncode not in (0, 1):
            raise ValidationExecutionError(
                result.stderr.strip() or f"veraPDF exited with status {result.returncode}"
            )
        return parsed
