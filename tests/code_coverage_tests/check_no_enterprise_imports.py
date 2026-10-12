import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

FORBIDDEN_ROOTS: Final = frozenset({"enterprise", "litellm_enterprise"})


def _imported_modules(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            yield node.lineno, node.module


def find_enterprise_imports(root: Path) -> tuple[str, ...]:
    return tuple(
        f"{path}:{line}: {module}"
        for path in sorted(root.rglob("*.py"))
        for line, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if module.split(".")[0] in FORBIDDEN_ROOTS
    )


if __name__ == "__main__":
    violations: Final = find_enterprise_imports(Path("litellm"))
    if violations:
        print("Enterprise imports are not allowed in this codebase:")
        print("\n".join(violations))
        sys.exit(1)
    print("No enterprise imports found.")
