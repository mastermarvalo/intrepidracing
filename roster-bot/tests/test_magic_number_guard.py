"""
The ADR-001 rule-1 guard flags every numeric literal outside {0, 1, -1}
inside bot/market/, bot/contracts/, and bot/valuation.py — and only
there. It handles unary-minus literals so `-1` reads as -1 (not "1 with
a minus in front"), skips booleans (they subclass int), and passes clean
on files that only use allowed literals.

These tests hand the guard synthetic source trees rather than checking
the live tree, so the tests keep passing after Phase 2+ add real code
under the guarded modules.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_magic_numbers.py"


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _write_guarded_file(tmp_path: Path, rel: str, body: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body))


def _patched_script(tmp_path: Path) -> Path:
    """
    Copy the real guard script into tmp_path/scripts/ so its
    ``REPO_ROOT = Path(__file__).resolve().parent.parent`` resolves to
    ``tmp_path`` — the tests then exercise the real walker against the
    synthetic ``tmp_path/bot/...`` tree.
    """
    src = SCRIPT.read_text()
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "scripts" / "check_magic_numbers.py"
    target.write_text(src)
    return target


def test_passes_when_no_guarded_files_exist(tmp_path):
    script = _patched_script(tmp_path)
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr


def test_allows_zero_one_negative_one(tmp_path):
    script = _patched_script(tmp_path)
    _write_guarded_file(
        tmp_path,
        "bot/market/example.py",
        """
        def bump(x):
            if x == 0:
                return x + 1
            return x - 1
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr


def test_rejects_arbitrary_int_literal(tmp_path):
    script = _patched_script(tmp_path)
    _write_guarded_file(
        tmp_path,
        "bot/market/example.py",
        """
        SALARY_CAP = 145
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "145" in result.stderr


def test_rejects_float_literal(tmp_path):
    script = _patched_script(tmp_path)
    _write_guarded_file(
        tmp_path,
        "bot/contracts/example.py",
        """
        WEEKLY_CAP = 0.75
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "0.75" in result.stderr


def test_rejects_negative_int_beyond_minus_one(tmp_path):
    script = _patched_script(tmp_path)
    _write_guarded_file(
        tmp_path,
        "bot/valuation.py",
        """
        DECAY = -2
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "-2" in result.stderr


def test_ignores_presets_directory(tmp_path):
    script = _patched_script(tmp_path)
    # bot/presets/ is DATA, not business logic — allowed to contain
    # literals freely. The guard should never look there.
    _write_guarded_file(
        tmp_path,
        "bot/presets/f1.py",
        """
        SALARY_CAP = 145.0
        WEEKLY_CAP = 0.75
        MAX_TERM = 3
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr


def test_booleans_not_flagged(tmp_path):
    script = _patched_script(tmp_path)
    # `True` and `False` subclass `int` — guard must not treat them as
    # numeric literals.
    _write_guarded_file(
        tmp_path,
        "bot/market/flags.py",
        """
        FEATURE_ON = True
        FEATURE_OFF = False
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr


def test_decimal_string_literals_not_flagged(tmp_path):
    script = _patched_script(tmp_path)
    # Decimal("1.25") is a *string* literal; guard should not flag it.
    _write_guarded_file(
        tmp_path,
        "bot/contracts/rules.py",
        """
        from decimal import Decimal
        LIMIT = Decimal("1.25")
        """,
    )
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr
