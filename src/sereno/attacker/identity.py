"""Fingerprint the runtime package and the local source modules it imports."""

import ast
import hashlib
from pathlib import Path


def code_sources() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    pending = list((root / "attacker").rglob("*.py"))
    parsed: set[Path] = set()
    sources: set[Path] = set()
    while pending:
        source = pending.pop()
        if source in parsed:
            continue
        parsed.add(source)
        sources.add(source)
        for parent in (source.parent, *source.parent.parents):
            if not parent.is_relative_to(root):
                break
            initializer = parent / "__init__.py"
            if initializer.is_file():
                sources.add(initializer)
        tree = ast.parse(source.read_bytes(), filename=str(source))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.append(node.module)
                names.extend(f"{node.module}.{alias.name}" for alias in node.names)
        for name in names:
            if not name.startswith("sereno."):
                continue
            relative = Path(*name.split(".")[1:])
            for dependency in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
                if dependency.is_file() and dependency not in parsed:
                    pending.append(dependency)
    return sorted(sources)


def code_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for source in code_sources():
        digest.update(str(source.relative_to(root)).encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()
