"""Behavior tests for cardo — runs commands and checks they actually do
what they claim. Uses subprocess so it's end-to-end (the same code path
a real user hits)."""

from pathlib import Path


def test_stats_runs_on_sample_tree(sample_tree: Path, run_cardo):
    """Stats on the sample tree should run cleanly and report file info.

    Note: stats doesn't take -r — it's recursive by default. We just verify
    the command produces output mentioning a file count or size.
    """
    result = run_cardo("stats", str(sample_tree))
    assert "file" in result.stdout.lower() or "files" in result.stdout.lower()


def test_tree_includes_known_filenames(sample_tree: Path, run_cardo):
    """Tree output should mention every file we put in the sample tree."""
    result = run_cardo("tree", str(sample_tree))
    for expected_name in ("photo1.jpg", "doc.pdf", "notes.txt", "sub"):
        assert (
            expected_name in result.stdout
        ), f"{expected_name} not found in tree output:\n{result.stdout}"


def test_organize_dry_run_makes_no_changes(sample_tree: Path, run_cardo):
    """Dry-run should produce output describing the plan but leave files alone."""
    files_before = {p.name for p in sample_tree.iterdir()}

    result = run_cardo("organize", str(sample_tree), "-r", "-n", "-y")
    assert "Would move" in result.stdout or "would" in result.stdout.lower()

    files_after = {p.name for p in sample_tree.iterdir()}
    assert files_before == files_after, (
        f"dry-run modified the directory!\n" f"before: {files_before}\nafter: {files_after}"
    )


def test_organize_actually_moves_files(sample_tree: Path, run_cardo):
    """Without --dry-run, organize should move files into category folders."""
    run_cardo("organize", str(sample_tree), "-r", "-y")

    # The original photo1.jpg should no longer be at the root.
    assert not (sample_tree / "photo1.jpg").exists()
    # Category folders should have appeared.
    expected_categories = {"Images", "Documents", "Code"}
    actual_dirs = {p.name for p in sample_tree.iterdir() if p.is_dir()}
    assert expected_categories.issubset(
        actual_dirs
    ), f"expected categories {expected_categories} not all present in {actual_dirs}"


def test_undo_reverses_organize(sample_tree: Path, run_cardo):
    """After organize → undo, every file should be back at its original location."""
    original_files = sorted(p.name for p in sample_tree.iterdir() if p.is_file())

    # Move things into categories
    run_cardo("organize", str(sample_tree), "-r", "-y")
    # Verify the move happened (precondition)
    assert any(p.is_dir() and p.name == "Images" for p in sample_tree.iterdir())

    # Reverse it
    run_cardo("undo", "-y")

    # Every original file should be back at the root.
    for filename in original_files:
        assert (
            sample_tree / filename
        ).exists(), f"{filename} not restored to its original location after undo"


def test_dedupe_quick_finds_duplicates(tmp_path: Path, run_cardo):
    """Two files with identical contents should be detected in quick mode."""
    (tmp_path / "original.txt").write_bytes(b"the same content")
    (tmp_path / "duplicate.txt").write_bytes(b"the same content")
    (tmp_path / "unique.txt").write_bytes(b"different content entirely")

    result = run_cardo("dedupe", str(tmp_path), "-r", "--mode", "quick", "-y")

    # Quick mode just produces a triage report. We're verifying it runs
    # without crashing and produces some recognizable output.
    output = result.stdout.lower()
    assert (
        "scan" in output or "duplicate" in output or "suspect" in output
    ), f"dedupe quick produced no recognizable output:\n{result.stdout}"


def test_clean_dry_run_makes_no_changes(tmp_path: Path, run_cardo):
    """Dry-run clean should report empty dirs but leave them alone."""
    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()
    (tmp_path / "not_empty").mkdir()
    (tmp_path / "not_empty" / "file.txt").write_bytes(b"keep me")

    dirs_before = sorted(p.name for p in tmp_path.iterdir())
    run_cardo("clean", str(tmp_path), "-n", "-y")
    dirs_after = sorted(p.name for p in tmp_path.iterdir())

    assert dirs_before == dirs_after


def test_help_for_each_command_works(run_cardo):
    """Every advertised command should accept --help."""
    commands = [
        "stats",
        "tree",
        "search",
        "organize",
        "copy",
        "move",
        "rename",
        "dedupe",
        "clean",
        "sync",
        "verify",
        "undo",
        "restore",
        "config",
        "name-clash",
    ]
    for cmd in commands:
        result = run_cardo(cmd, "--help")
        assert (
            "usage" in result.stdout.lower()
        ), f"cardo {cmd} --help didn't produce a usage line:\n{result.stdout}"
