#!/usr/bin/env python3
"""
ADR-001 rule-1 guard: fail if any numeric literal other than 0, 1, or -1
appears in the core market/contracts/valuation code.

Rationale: salary caps, movement caps, min/max salary, contract term
limits, roster slot counts, incentive ceilings, valuation weights, and
points tables must live in DB config rows seeded by a preset — never as
Python literals in business logic. Presets in bot/presets/ are data, so
they are allowed to contain literals; this script excludes them.

Exit codes:
  0 — clean
  1 — at least one violation

Run manually with `python scripts/check_magic_numbers.py`, or via
`make check-magic` from the Makefile.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Business-logic modules that must be free of magic numbers.
GUARDED = [
    REPO_ROOT / "bot" / "market",
    REPO_ROOT / "bot" / "contracts",
    REPO_ROOT / "bot" / "valuation.py",
]

# Only these numeric values are always allowed anywhere.
ALLOWED_LITERALS: set[int | float] = {0, 1, -1}


def _iter_target_files() -> list[Path]:
    files: list[Path] = []
    for target in GUARDED:
        if not target.exists():
            continue
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


def _numeric_value(node: ast.AST) -> int | float | None:
    """
    Return the numeric value at `node`, folding a leading unary minus so
    that `-1` (`UnaryOp(USub, Constant(1))`) is recognised as -1.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric_value(node.operand)
        if inner is not None:
            return -inner
    return None


def _find_violations(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[tuple[int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._skip: set[int] = set()

        def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
            value = _numeric_value(node)
            if value is not None:
                if value not in ALLOWED_LITERALS:
                    violations.append((node.lineno, repr(value)))
                # Skip the wrapped Constant so we don't double-count.
                if isinstance(node.operand, ast.Constant):
                    self._skip.add(id(node.operand))
                return
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if id(node) in self._skip:
                return
            if isinstance(node.value, bool):
                return
            if isinstance(node.value, (int, float)):
                if node.value not in ALLOWED_LITERALS:
                    violations.append((node.lineno, repr(node.value)))

    Visitor().visit(tree)
    return violations


def main() -> int:
    files = _iter_target_files()
    if not files:
        # Nothing to guard yet (Phase 1 lands the guard before the
        # guarded modules exist). Passing silently is the right answer.
        return 0

    total = 0
    for path in files:
        for lineno, value in _find_violations(path):
            rel = path.relative_to(REPO_ROOT)
            print(
                f"{rel}:{lineno}: magic number {value} — move to DB config or a preset",
                file=sys.stderr,
            )
            total += 1

    if total:
        print(
            f"\n{total} magic-number violation(s). See ADR-001 rule 1.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
