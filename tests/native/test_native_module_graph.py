from __future__ import annotations

import ast
import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "pdf2pdfa" / "native"


def _modules() -> set[str]:
    result: set[str] = set()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.name == "__init__.py":
            if relative.parent != Path("."):
                result.add(".".join(relative.parent.parts))
            continue
        result.add(".".join(relative.with_suffix("").parts))
    return result


def test_every_relative_native_import_resolves_to_owned_source():
    available = _modules()
    failures: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package_parts = list(path.relative_to(ROOT).with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level <= 0:
                continue
            # All current native modules are top-level. Keep the resolver
            # generic enough for future subpackages.
            ascend = max(0, node.level - 1)
            base = package_parts[: len(package_parts) - ascend] if ascend else package_parts
            if node.module:
                target_parts = base + node.module.split(".")
                target = ".".join(target_parts)
                if target not in available and not (ROOT / Path(*target_parts)).is_dir():
                    failures.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: missing .{node.module}"
                    )
    assert not failures, "broken owned relative imports:\n" + "\n".join(failures)


def test_every_native_module_imports_without_optional_runtime_packages():
    # This catches syntax errors, circular import mistakes and missing owned
    # modules. Importing modules is safe: native modules perform no file/network
    # operations at import time.
    failures: list[str] = []
    for module in sorted(_modules()):
        try:
            importlib.import_module("pdf2pdfa.native." + module)
        except Exception as exc:
            failures.append(f"{module}: {type(exc).__name__}: {exc}")
    assert not failures, "owned module import failures:\n" + "\n".join(failures)
