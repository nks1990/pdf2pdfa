from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path
import re
import sys

import pdf2pdfa


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "pdf2pdfa"


def test_runtime_package_imports_only_stdlib_or_owned_modules():
    allowed = set(sys.stdlib_module_names) | {"pdf2pdfa", "__future__"}
    failures: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in allowed:
                        failures.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".", 1)[0]
                if root not in allowed:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: from {node.module} import ..."
                    )
    assert not failures, "non-owned runtime imports:\n" + "\n".join(failures)


def test_every_distributed_python_module_imports():
    failures: list[str] = []
    modules = [pdf2pdfa.__name__]
    modules.extend(
        item.name
        for item in pkgutil.walk_packages(pdf2pdfa.__path__, pdf2pdfa.__name__ + ".")
    )
    for module in sorted(set(modules)):
        try:
            importlib.import_module(module)
        except Exception as exc:
            failures.append(f"{module}: {type(exc).__name__}: {exc}")
    assert not failures, "package import failures:\n" + "\n".join(failures)


def test_pyproject_declares_zero_runtime_dependencies():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", text, re.S)
    assert match is not None, "pyproject.toml must declare dependencies explicitly"
    assert not match.group(1).strip(), "runtime dependencies must remain empty"


def test_external_runtime_engines_are_absent_from_package_sources():
    forbidden = {
        "ghostscript": "external renderer/converter",
        "verapdf": "external PDF/A validator",
        "pikepdf": "third-party PDF engine",
        "fonttools": "third-party font engine",
        "from PIL": "third-party image engine",
        "import click": "third-party CLI framework",
    }
    failures: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        for token, reason in forbidden.items():
            if token.lower() in lowered:
                failures.append(f"{path.relative_to(ROOT)}: {reason} token {token!r}")
    assert not failures, "external runtime engine references:\n" + "\n".join(failures)
