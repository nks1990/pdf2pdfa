from __future__ import annotations

import ast
from pathlib import Path
import sys


_ALLOWED_TOP_LEVEL = set(sys.stdlib_module_names) | {"__future__"}


def test_native_engine_has_no_third_party_imports():
    root = Path(__file__).resolve().parents[2] / "pdf2pdfa" / "native"
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top not in _ALLOWED_TOP_LEVEL:
                        violations.append(f"{path.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                if node.module:
                    top = node.module.split(".", 1)[0]
                    if top not in _ALLOWED_TOP_LEVEL:
                        violations.append(
                            f"{path.name}:{node.lineno}: from {node.module} import ..."
                        )
    assert not violations, "native engine imported third-party code:\n" + "\n".join(violations)
