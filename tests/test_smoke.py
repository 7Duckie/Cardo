"""Smoke tests for cardo: things that should always work."""

import subprocess
import sys
from pathlib import Path

# Path to the cardo.py we're testing — one level up from this tests/ folder.
CARDO_PY = Path(__file__).parent.parent / "cardo.py"


def test_cardo_file_exists():
    """The cardo.py we're trying to test should actually exist."""
    assert CARDO_PY.exists(), f"cardo.py not found at {CARDO_PY}"


def test_help_exits_cleanly():
    """`cardo --help` should print help and exit with code 0."""
    result = subprocess.run(
        [sys.executable, str(CARDO_PY), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "cardo" in result.stdout.lower()


def test_no_arguments_shows_welcome():
    """Running `cardo` with no arguments should print our welcome message."""
    result = subprocess.run(
        [sys.executable, str(CARDO_PY)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Cardo" in result.stdout
    assert "command" in result.stdout.lower() or "help" in result.stdout.lower()
