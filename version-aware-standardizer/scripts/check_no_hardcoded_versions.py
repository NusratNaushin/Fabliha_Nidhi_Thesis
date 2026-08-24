"""Refuse a terminology release identifier baked into executable code.

    python scripts/check_no_hardcoded_versions.py
    python scripts/check_no_hardcoded_versions.py --path backend --path scripts

Hard Rules 1-3 say a LOINC or SNOMED release identifier must always come from
import metadata, never from a constant in the source tree.  That rule is easy to
state and easy to break by accident -- a "temporary" ``VERSION = "2.82"`` is
exactly how a version-aware system quietly stops being version-aware.

What counts as a violation: a string or numeric literal that looks like a LOINC
version (``2.82``) or a SNOMED release date (``20260801``) and that a reader of
the *running program* would see.

What does not: docstrings, comments, and argparse ``help=`` text, because citing
an example in prose is how the CLI explains itself.  Those are excluded
structurally rather than by pattern, so the check cannot be fooled by moving a
literal around.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PATHS = ("backend", "scripts")

# 2.82 / 2.83 style LOINC versions, and YYYYMMDD SNOMED release dates.
LOINC_VERSION = re.compile(r"(?<!\d)2\.\d{2}(?!\d)")
SNOMED_DATE = re.compile(r"(?<!\d)(19|20)\d{6}(?!\d)")

# Files that legitimately talk about release identifiers.
EXCLUDED_NAMES = {
    # This checker itself contains the patterns it looks for.
    "check_no_hardcoded_versions.py",
}


class Violation:
    def __init__(self, path: Path, line: int, value: str, context: str) -> None:
        self.path = path
        self.line = line
        self.value = value
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.path.as_posix()}:{self.line}: {self.value!r} "
            f"(in {self.context})"
        )


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every constant that is a module/class/function docstring."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return docstrings


def _help_text_nodes(tree: ast.AST) -> set[int]:
    """id() of every constant passed as ``help=`` or ``description=``.

    CLI help legitimately cites an example version -- that is the string that
    teaches a user what to type.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in {"help", "description", "epilog", "metavar"}:
                for inner in ast.walk(keyword.value):
                    if isinstance(inner, ast.Constant):
                        allowed.add(id(inner))
    return allowed


def _matches(value: str) -> str | None:
    match = LOINC_VERSION.search(value) or SNOMED_DATE.search(value)
    return match.group(0) if match else None


def scan_source(source: str, path: Path) -> list[Violation]:
    """Every release-identifier literal in ``source`` that a reader would run."""
    tree = ast.parse(source, filename=str(path))
    excluded = _docstring_nodes(tree) | _help_text_nodes(tree)

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in excluded:
            continue
        value = node.value
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            found = _matches(value)
            context = "string literal"
        elif isinstance(value, (int, float)):
            found = _matches(repr(value))
            context = "numeric literal"
        else:
            continue
        if found:
            violations.append(Violation(path, node.lineno, found, context))
    return violations


def scan_path(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file in files:
        if file.name in EXCLUDED_NAMES:
            continue
        try:
            source = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"WARNING: could not read {file}: {exc}", file=sys.stderr)
            continue
        try:
            violations.extend(scan_source(source, file))
        except SyntaxError as exc:
            print(f"WARNING: could not parse {file}: {exc}", file=sys.stderr)
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        default=None,
        help="directory or file to scan (repeatable; default: backend and scripts)",
    )
    args = parser.parse_args(argv)

    targets = [Path(p) for p in (args.path or DEFAULT_PATHS)]
    resolved = [p if p.is_absolute() else ROOT / p for p in targets]

    missing = [p for p in resolved if not p.exists()]
    if missing:
        for path in missing:
            print(f"ERROR: {path} does not exist.", file=sys.stderr)
        return 2

    violations: list[Violation] = []
    for path in resolved:
        violations.extend(scan_path(path))

    scanned = ", ".join(p.name for p in resolved)
    if violations:
        print(f"Hard-coded release identifier(s) found in {scanned}:")
        for violation in violations:
            print(f"  {violation}")
        print()
        print(
            "A release version must come from import metadata, user input or file\n"
            "metadata (Hard Rules 1-3). If this literal is documentation, move it\n"
            "into a docstring or an argparse help= string."
        )
        return 1

    print(f"No hard-coded release identifiers in executable code ({scanned}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
