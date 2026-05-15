"""Shared pytest fixtures for cardo tests.

This file is auto-discovered by pytest. Any fixture defined here is
available to every test in this directory and its subdirectories without
explicit import. That's a pytest convention — `conftest.py` is special.
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Path to the cardo.py we're testing — one level up from the tests/ folder.
CARDO_PY = Path(__file__).parent.parent / "cardo.py"


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A small, predictable directory tree for behavior tests.

    Layout:
        tmp/
        ├── photo1.jpg     (4 bytes)
        ├── photo2.jpg     (4 bytes)
        ├── doc.pdf        (8 bytes)
        ├── notes.txt      (8 bytes)
        └── sub/
            └── code.py    (12 bytes)
    """
    (tmp_path / "photo1.jpg").write_bytes(b"img1")
    (tmp_path / "photo2.jpg").write_bytes(b"img2")
    (tmp_path / "doc.pdf").write_bytes(b"document")
    (tmp_path / "notes.txt").write_bytes(b"some text")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "code.py").write_bytes(b"print('hi')")
    return tmp_path


@pytest.fixture
def run_cardo():
    """Helper that runs cardo as a subprocess and returns the result.

    Usage in a test:
        result = run_cardo("stats", path, "-r")
        assert result.returncode == 0
        assert "Documents" in result.stdout
    """

    def _run(*args: str, expect_success: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(CARDO_PY), *args],
            capture_output=True,
            text=True,
        )
        if expect_success and result.returncode != 0:
            # Show the failure clearly when a test expected success.
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            raise AssertionError(f"cardo {' '.join(args)} failed with code {result.returncode}")
        return _run_returns_completed_process(result)

    return _run


def _run_returns_completed_process(result):
    """Identity helper — exists only so the type annotation reads naturally."""
    return result
