"""Shared helpers for unit tests and the benchmark (no network)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

SKIP_HASH_NAMES = {".git", ".venv", "__pycache__", ".pytest_cache", "reports"}


def tree_sha256(root: Path) -> str:
    """Stable hash of file paths and contents. Skips VCS/cache dirs."""
    root = root.resolve()
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_HASH_NAMES for part in rel.parts):
            continue
        files.append(path)
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def is_readonly_file(path: Path) -> bool:
    mode = path.stat().st_mode
    return not (mode & stat.S_IWUSR or mode & stat.S_IWGRP or mode & stat.S_IWOTH)


def write_calculator(root: Path, *, with_divide: bool = True) -> None:
    body = [
        "def add(left: int, right: int) -> int:",
        "    return left + right",
        "",
    ]
    if with_divide:
        body += [
            "def divide(left: int, right: int) -> float:",
            "    if right == 0:",
            '        raise ValueError("division by zero")',
            "    return left / right",
            "",
        ]
    (root / "calculator.py").write_text("\n".join(body), encoding="utf-8")


def write_docs(root: Path, text: str) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "overview.md").write_text(text, encoding="utf-8")


def write_tests(
    root: Path,
    *,
    failing: bool = False,
    dangerous: bool = False,
    include: bool = True,
) -> None:
    if not include:
        return
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    if dangerous:
        content = '''import shutil
from pathlib import Path

def test_destructive():
    target = Path("scratch_dir")
    target.mkdir(exist_ok=True)
    (target / "x.txt").write_text("x", encoding="utf-8")
    shutil.rmtree(target)

def test_network():
    import urllib.request
    urllib.request.urlopen("http://127.0.0.1:1", timeout=1)
'''
    elif failing:
        content = '''import calculator

def test_add():
    assert calculator.add(2, 3) == 0
'''
    else:
        content = '''import pytest
import calculator

def test_add():
    assert calculator.add(2, 3) == 5

def test_divide():
    if hasattr(calculator, "divide"):
        assert calculator.divide(8, 2) == 4

def test_divide_rejects_zero():
    if hasattr(calculator, "divide"):
        with pytest.raises(ValueError):
            calculator.divide(1, 0)
'''
    (tests / "test_calculator.py").write_text(content, encoding="utf-8")


def mini_project(
    root: Path,
    *,
    with_divide: bool = True,
    with_tests: bool = True,
    failing: bool = False,
    dangerous: bool = False,
    docs: str | None = None,
    secret: bool = False,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_calculator(root, with_divide=with_divide)
    write_tests(root, failing=failing, dangerous=dangerous, include=with_tests)
    default_docs = (
        "# Sample calculator\n\n"
        "Python helpers `add` and `divide`.\n\n"
        "## Behavior\n\n"
        "- `add` returns the sum of two integers.\n"
        "- `divide` returns a float quotient.\n"
        "- `divide` must reject a zero divisor with `ValueError`.\n"
    )
    write_docs(root, docs if docs is not None else default_docs)
    if secret:
        (root / ".env").write_text("SECRET_TOKEN=do-not-copy\n", encoding="utf-8")
    return root
