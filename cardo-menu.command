#!/usr/bin/env python3
"""
cardo-menu — interactive menu wrapper around cardo.py for macOS double-click use.

Lives next to cardo.py and presents a numbered menu of operations. Designed
to be saved as a .command file so macOS will open it in Terminal on double-click.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
FILEMGR = HERE / "cardo.py"
LOG_DIR = Path.home() / ".cardo" / "logs"
CONFIG_FILE = Path.home() / ".cardo" / "config.toml"

# Detect whether send2trash is importable. This determines whether the menu
# offers the "Use trash?" prompt for dedupe and clean. Mirror of the same
# check in cardo.py — kept independent so the menu degrades gracefully if
# cardo.py changes its import discipline.
try:
    import send2trash  # noqa: F401
    _TRASH_AVAILABLE = True
except ImportError:
    _TRASH_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────
# Menu definition
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str  # cardo subcommand, or special "_xxx" actions handled in main()


MENU: list[MenuItem] = [
    MenuItem("Stats — size/count breakdown of a folder",      "stats"),
    MenuItem("Tree — show folder structure",                  "tree"),
    MenuItem("Search — find files by name, size, or age",     "search"),
    MenuItem("Name clash — find files sharing a name",        "name-clash"),
    MenuItem("Organize — sort files into category subfolders","organize"),
    MenuItem("Copy — copy files to another folder",           "copy"),
    MenuItem("Move — move files to another folder",           "move"),
    MenuItem("Rename — bulk-rename files",                    "rename"),
    MenuItem("Dedupe — find/remove duplicate files",          "dedupe"),
    MenuItem("Clean — remove empty subdirectories",           "clean"),
    MenuItem("Undo — reverse the last move/rename/organize/clean", "undo"),
    MenuItem("View recent logs",                              "_view_logs"),
    MenuItem("Config — show or create the config file",       "_config"),
    MenuItem("Help — browse explanations of menu options",    "_help"),
]

# Operations that write to disk → auto-enable logging.
WRITING_ACTIONS = {"copy", "move", "rename", "dedupe", "organize", "clean"}
# Read-only operations that produce findings → offer an HTML report.
REPORTABLE_READONLY = {"name-clash", "search", "stats", "tree"}
# Operations that delete things → offer the trash option (if available).
DELETING_ACTIONS = {"dedupe", "clean"}


# ──────────────────────────────────────────────────────────────────────────
# Help system
#
# Every prompt has an associated help "topic". Typing '?' or 'help' at any
# prompt shows that topic's text and re-prompts for input. Help is OFF for
# the very first menu choice to keep new users from getting stuck — they
# can still get help by reading the inline tip below the menu.
# ──────────────────────────────────────────────────────────────────────────

HELP_TRIGGERS = frozenset({"?", "help", "h", "/?"})

HELP_TOPICS: dict[str, str] = {
    "main_menu": """\
  ─── cardo help ───

  cardo is a file-cleanup toolkit. Each menu option starts a different kind
  of operation:

    Read-only (nothing on disk changes):
      Stats      — size/count breakdown by file category
      Tree       — folder tree printout
      Search     — find files by name, size, or age
      Name clash — find files with identical names across the tree
      View logs  — review past run logs
      Config     — show or create your config file

    Write operations (these change files; they all support 'dry run'):
      Organize — sort files into subfolders by type
      Copy     — copy files (or whole subtrees with -r)
      Move     — move files
      Rename   — bulk rename with prefix/suffix/lowercase
      Dedupe   — find/remove duplicate files (three speed modes)
      Clean    — remove empty directories

    Recovery:
      Undo — reverse the last move/rename/organize/clean run

  Safety: write operations refuse to touch installed-application content
  (Adobe, Cinema 4D, .app bundles, etc.) by default. You'll get a summary
  of what was detected and a single 'Proceed?' prompt — answer 'n' to bail.

  Type a number to start, 'q' to quit.
  Press Enter on a blank line at any prompt to cancel back to the menu.
""",
    "path": """\
  ─── Folder selection help ───

  Type or paste an absolute path, e.g.:
    /Users/yourname/Pictures
    /Volumes/MyDrive
    ~/Downloads             (~ expands to your home folder)

  Easier: switch to Finder, find the folder, and DRAG IT into the Terminal
  window. macOS automatically pastes the path with proper quoting.

  Press Enter on an empty line to cancel and return to the main menu.
""",
    "yesno": """\
  ─── Yes/No prompts ───

  Type 'y' or 'yes' for yes, anything else (or just Enter) for the default.
  The capitalization in '[y/N]' or '[Y/n]' tells you the default:
    [y/N] — capital N means 'no' is the default
    [Y/n] — capital Y means 'yes' is the default

  Press Enter to accept the default.
""",
    "generic": """\
  ─── This prompt ───

  Type a value to use, or press Enter to keep the default (shown in
  square brackets, like [10]).

  For filters that accept blank, leaving it empty skips that filter.
""",
    "dedupe_mode": """\
  ─── Dedupe scan modes ───

  Quick    — Walks the filesystem and groups files by name + size only.
             No hashing, no deletion. Fastest mode (seconds to minutes).
             Use this for triage: see if a real scan is worth running.

  Standard — Stages: prefix-hash → full SHA-256. Mathematically certain
             that flagged duplicates are byte-identical. Safe to delete.
             Recommended for almost all real cleanup work.

  Paranoid — Standard plus a final byte-by-byte comparison before each
             deletion. Slowest, but verifies files truly match before
             unlinking them. Use for irreplaceable data.

  Speed (rough): Quick is seconds, Standard is minutes to hours depending
  on disk size, Paranoid is ~30-50% slower than Standard.
""",
    "dry_run": """\
  ─── Dry run ───

  A dry run shows you exactly what the command WOULD do without actually
  changing anything. No files are moved, renamed, or deleted.

  Strongly recommended for first-time use on a new folder. After reviewing
  the planned actions, re-run with the same answers but say 'no' to dry
  run to commit the changes.
""",
    "report": """\
  ─── HTML report ───

  Saves the findings to a self-contained HTML file in:
    ~/.cardo/reports/

  Open it in any browser to review later or share with someone else.
  Useful for read-only commands (search, stats, name-clash, tree) and
  for dedupe scans where you want to study the duplicates before deleting.

  Costs a tiny bit of extra time, no other downsides.
""",
    "recursive": """\
  ─── Recursive / include subfolders ───

  Without this, only files DIRECTLY inside the chosen folder are touched.
  Subfolders are left alone.

  With this, the command descends into every subfolder. Necessary for
  most use cases (e.g. organizing your whole Downloads tree), but it
  means more files will be affected, so use dry-run first.
""",
    "trash": """\
  ─── Send to trash vs. delete permanently ───

  YES (trash) — files go to the OS Trash and can be recovered from there
  by you (or by Finder / your file manager) until you empty it. The
  recommended choice when you're not 100% sure.

  NO (permanent) — files are unlinked immediately. Faster, no recoverability.
  Choose this only when you definitely don't want to keep the option to
  restore.

  The trash option requires the `send2trash` Python package. If it's not
  installed this prompt won't appear and operations will permanently delete.
  To install it:  pip install send2trash
""",
    "undo": """\
  ─── Undo ───

  Reverses the most recent reversible run. Reversible operations are:
    Move      → moves files back to their original locations
    Rename    → restores original filenames
    Organize  → moves files out of category folders
    Clean     → recreates the empty directories that were removed

  Dedupe deletions can't be undone via this menu — but if you ran dedupe
  with 'Use trash' enabled, the files are in your OS Trash and you can
  restore them from there.

  Each undo consumes the run from the queue; running undo twice in a row
  reverses the two most recent runs (in newest-first order).
""",
    "config": """\
  ─── Config file ───

  cardo supports an optional config file at:
    ~/.cardo/config.toml

  It lets you set defaults for things you'd otherwise type every time:
    • Skip the 'Proceed?' prompt
    • Auto-save HTML reports
    • Auto-write log files
    • Default trash mode (instead of permanent delete)
    • Custom file-type categories for Organize/Stats
    • Default dedupe mode and minimum size

  The menu offers two actions:
    Show — print effective settings and whether the file exists
    Init — write a commented starter file you can edit

  CLI flags always override config values, so the menu's per-prompt
  questions still take precedence over what's in the file.
""",
    "protection": """\
  ─── Installation-folder protection ───

  cardo's destructive operations (clean, organize, move, rename, dedupe)
  detect installed applications and refuse to touch their contents by
  default. Detection covers:

    • macOS app/framework/bundle suffixes (.app, .framework, .lrdata, etc.)
    • Vendor-style installation folders (Adobe, Maxon Cinema 4D, JetBrains,
      Unity, Lightroom, MagicQ, and similar)
    • Reverse-DNS layouts (com.*, net.maxon.*, etc.)

  When the protection fires, you'll see a summary like:

    ⚠ Protection: skipping 8 action(s) that would touch installed-application
      content:
           4× inside installation folder 'Adobe InDesign 2026'
           2× inside installation folder 'Maxon Cinema 4D 2024'
           2× inside .app package

      First 5 of 8:
        • /path/to/Adobe InDesign 2026/Plug-Ins
        • ...

  Then a single prompt: "Proceed? [y/N]".

    YES — runs the safe actions, skips the protected ones
    NO  — aborts the whole run, nothing is changed

  The protection is on by default and there is no menu option to disable it.
  If you genuinely need to operate inside an installed application (very rare;
  e.g. removing your own files from a plug-ins folder), run cardo from the
  CLI with --include-unsafe.

  If you point any destructive operation directly AT an installed application
  folder, you'll also get a warning about the folder itself before the
  protection prompt — read it carefully and bail out if the path wasn't what
  you intended.
""",
}


def show_help(topic: str) -> None:
    """Print the help block for `topic`, falling back to main_menu help."""
    text = HELP_TOPICS.get(topic) or HELP_TOPICS["main_menu"]
    print()
    print(text)


# ──────────────────────────────────────────────────────────────────────────
# Input helpers
#
# `ask` is the workhorse. It intercepts '?' / 'help' for context-sensitive
# help and re-prompts. Everything else (prompt_path, prompt_optional,
# confirm) goes through ask().
# ──────────────────────────────────────────────────────────────────────────

def ask(prompt: str, topic: str = "main_menu") -> str:
    """input() wrapper that intercepts '?' or 'help' to display context help.

    Loops until the user enters something that isn't a help trigger. EOF and
    Ctrl+C re-raise so the caller can handle them as a cancel/quit signal.
    """
    while True:
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise
        stripped = raw.strip()
        if stripped.lower() in HELP_TRIGGERS:
            show_help(topic)
            continue
        return stripped


def prompt_path(label: str) -> str | None:
    """Ask for a folder path. Accepts drag-and-drop from Finder.

    macOS Terminal pastes dragged paths with backslash-escaped spaces,
    sometimes wrapped in single quotes. shlex.split handles both.
    Returns None if the user enters a blank line (= cancel).
    """
    raw = ask(f"  {label} (or drag a folder here, blank to cancel): ", topic="path")
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = [raw]
    return parts[0] if parts else None


def prompt_optional(label: str, default: str = "", topic: str = "generic") -> str:
    val = ask(f"  {label} [{default}]: ", topic=topic)
    return val or default


def confirm(label: str, default_no: bool = True, topic: str = "yesno") -> bool:
    suffix = "[y/N]" if default_no else "[Y/n]"
    ans = ask(f"  {label} {suffix}: ", topic=topic).lower()
    if not ans:
        return not default_no
    return ans in ("y", "yes")


def ask_use_trash() -> bool:
    """Prompt for trash vs. permanent delete.

    Returns True if the caller should add --trash. If send2trash isn't
    available we don't ask (and return False) — there's no point offering
    a choice the user can't make. The script will permanently delete.
    """
    if not _TRASH_AVAILABLE:
        return False
    return confirm(
        "Send removed files to the OS trash? (recoverable)",
        default_no=False,
        topic="trash",
    )


# ──────────────────────────────────────────────────────────────────────────
# Recent logs viewer
# ──────────────────────────────────────────────────────────────────────────

def view_recent_logs() -> None:
    """List the 10 most recent log files and let the user pick one to view."""
    if not LOG_DIR.exists():
        print(f"\n  No logs yet.")
        print(f"  Logs are only created by operations that change files:")
        print(f"    copy, move, rename, dedupe, organize, clean")
        print(f"  Read-only operations (stats, tree, search, name-clash) don't create logs.")
        print(f"  Dry-run mode also doesn't create logs since nothing changes.")
        print(f"\n  Once you run a write operation, logs will appear in:")
        print(f"    {LOG_DIR}")
        return

    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        print(f"\n  No logs in {LOG_DIR}.")
        print(f"  Logs are only created by write operations (copy, move, rename,")
        print(f"  dedupe, organize, clean) — read-only commands don't produce them.")
        return

    print(f"\n  Recent logs:")
    visible = logs[:10]
    for i, log in enumerate(visible, start=1):
        size = log.stat().st_size
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(log.stat().st_mtime))
        print(f"    {i:>2}. {log.name}  ({size} bytes, {when})")
    print(f"     q. Cancel")
    try:
        choice = input("\n  View which? ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if choice in ("q", "quit", "") or not choice.isdigit():
        return
    idx = int(choice) - 1
    if 0 <= idx < len(visible):
        print("\n" + "─" * 60)
        print(visible[idx].read_text())
        print("─" * 60)
    else:
        print("  Not a valid choice.")


# ──────────────────────────────────────────────────────────────────────────
# Config submenu
# ──────────────────────────────────────────────────────────────────────────

def handle_config_menu() -> None:
    """Submenu for the config file: show, init, or open the path."""
    print()
    print(f"  Config file:    {CONFIG_FILE}")
    print(f"  File present:   {'yes' if CONFIG_FILE.exists() else 'no'}")
    print(f"  Trash support:  {'yes' if _TRASH_AVAILABLE else 'no (pip install send2trash)'}")
    print()
    print("  Options:")
    print("    1. Show effective settings")
    if CONFIG_FILE.exists():
        print("    2. Print path (so you can open it in a text editor)")
        print("    3. Overwrite with a fresh starter file")
    else:
        print("    2. Create a starter file you can edit")
    print("    q. Back to main menu")
    choice = ask("  Choose: ", topic="config").lower()
    if choice in ("q", "quit", ""):
        return
    print()
    if choice == "1":
        subprocess.run([sys.executable, str(FILEMGR), "config", "show"])
        return
    if choice == "2" and not CONFIG_FILE.exists():
        subprocess.run([sys.executable, str(FILEMGR), "config", "init"])
        return
    if choice == "2":
        subprocess.run([sys.executable, str(FILEMGR), "config", "path"])
        print()
        print(f"  Open that file in any text editor to make changes.")
        return
    if choice == "3" and CONFIG_FILE.exists():
        if confirm("Overwrite the existing config? (your edits will be lost)",
                   default_no=True):
            subprocess.run([sys.executable, str(FILEMGR), "config", "init", "--force"])
        else:
            print("  Cancelled.")
        return
    print("  Not a valid choice.")


# ──────────────────────────────────────────────────────────────────────────
# Help browser
# ──────────────────────────────────────────────────────────────────────────

# Topics exposed via the menu's "Help" item. Order matters here — it controls
# the listing the user sees. Each entry is (label, topic_key). The topic_key
# must exist in HELP_TOPICS.
HELP_BROWSER_TOPICS: list[tuple[str, str]] = [
    ("Overview — what cardo does",                    "main_menu"),
    ("Installation-folder protection (important)",      "protection"),
    ("Dry run — preview before committing",             "dry_run"),
    ("Send to trash vs. permanent delete",              "trash"),
    ("Undo — reversing the last run",                   "undo"),
    ("HTML reports",                                    "report"),
    ("Recursive / include subfolders",                  "recursive"),
    ("Dedupe scan modes",                               "dedupe_mode"),
    ("Folder selection (paths, drag & drop)",           "path"),
    ("Config file",                                     "config"),
]


def handle_help_browser() -> None:
    """Sub-menu that lets users read any help topic without hitting `?`
    at a specific prompt. Loops until the user picks 'q'."""
    while True:
        print()
        print("  ─── Help topics ───")
        for i, (label, _) in enumerate(HELP_BROWSER_TOPICS, start=1):
            print(f"  {i:>2}. {label}")
        print(f"   q. Back to main menu")
        try:
            choice = input("\n  Choose a topic: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("q", "quit", ""):
            return
        if not choice.isdigit():
            print("  Not a valid choice.")
            continue
        idx = int(choice) - 1
        if not (0 <= idx < len(HELP_BROWSER_TOPICS)):
            print("  Not a valid choice.")
            continue
        _, topic_key = HELP_BROWSER_TOPICS[idx]
        # show_help() already prints a blank line + the topic body.
        show_help(topic_key)
        try:
            input("  Press Enter to return to topics… ")
        except (EOFError, KeyboardInterrupt):
            return


# ──────────────────────────────────────────────────────────────────────────
# Per-action argument builders
#
# Each builder takes the partially-assembled argv list (already containing
# the subcommand) and either appends more args and returns it, or returns
# None if the user cancelled by leaving a path blank.
# ──────────────────────────────────────────────────────────────────────────

def _build_stats(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    return cmd


def _build_tree(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    depth = prompt_optional("Max depth (blank for unlimited)")
    if depth:
        cmd += ["--max-depth", depth]
    return cmd


def _build_clean(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    if ask_use_trash():
        cmd.append("--trash")
    if confirm("Dry run first (recommended)?", default_no=False, topic="dry_run"):
        cmd.append("-n")
    return cmd


def _build_search(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    pattern = prompt_optional("Name pattern, e.g. *.pdf (blank for any)")
    if pattern:
        cmd += ["-p", pattern]
    min_kb = prompt_optional("Min size in KB (blank to skip)")
    if min_kb:
        cmd += ["--min-size", min_kb]
    older = prompt_optional("Older than N days (blank to skip)")
    if older:
        cmd += ["--older-than", older]
    return cmd


def _build_name_clash(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    if confirm("Ignore case? (e.g. Photo.JPG == photo.jpg)"):
        cmd.append("--ignore-case")
    return cmd


def _build_organize(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder to organize")
    if not path:
        return None
    cmd.append(path)
    if confirm("Include subfolders (recursive)?", topic="recursive"):
        cmd.append("-r")
    if confirm("Dry run first (recommended)?", default_no=False, topic="dry_run"):
        cmd.append("-n")
    return cmd


def _build_copy_move(cmd: list[str]) -> list[str] | None:
    src = prompt_path("Source")
    if not src:
        return None
    dst = prompt_path("Destination")
    if not dst:
        return None
    cmd += [src, dst]
    if confirm("Include subfolders (recursive)?", topic="recursive"):
        cmd.append("-r")
    if confirm("Dry run first (recommended)?", default_no=False, topic="dry_run"):
        cmd.append("-n")
    return cmd


def _build_rename(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder")
    if not path:
        return None
    cmd.append(path)
    prefix = prompt_optional("Add prefix (blank to skip)")
    if prefix:
        cmd += ["--prefix", prefix]
    suffix = prompt_optional("Add suffix (blank to skip)")
    if suffix:
        cmd += ["--suffix", suffix]
    if confirm("Lowercase all names?"):
        cmd.append("--lower")
    if confirm("Dry run first (recommended)?", default_no=False, topic="dry_run"):
        cmd.append("-n")
    return cmd


def _build_dedupe(cmd: list[str]) -> list[str] | None:
    path = prompt_path("Folder to scan for duplicates")
    if not path:
        return None
    cmd += [path, "-r"]
    print()
    print("  Scan modes:")
    print("    1. Quick    — metadata only (no hashing, no deletion). Fast triage report.")
    print("    2. Standard — staged SHA-256 hashing. Safe to auto-delete. Recommended.")
    print("    3. Paranoid — staged hashing + byte-by-byte verification before each deletion.")
    choice = ask("  Choose mode [1/2/3, default 2] (or '?' for help): ",
                 topic="dedupe_mode") or "2"
    mode = {"1": "quick", "2": "standard", "3": "paranoid"}.get(choice, "standard")
    cmd += ["--mode", mode]
    if confirm("Save an HTML report when done?", topic="report"):
        cmd.append("--report")
    # Trash / dry-run only meaningful for modes that delete; quick is read-only.
    if mode in ("standard", "paranoid"):
        if ask_use_trash():
            cmd.append("--trash")
        if confirm("Dry run first (recommended for first-time scans)?",
                   default_no=False, topic="dry_run"):
            cmd.append("-n")
    return cmd


def _build_undo(cmd: list[str]) -> list[str] | None:
    """Walk the user through an undo. Offers dry-run preview, listing, or commit."""
    print()
    print("  Undo options:")
    print("    1. Preview (dry run) — show what would be reversed, change nothing")
    print("    2. List recent runs  — show what's available to undo")
    print("    3. Undo the most recent run now")
    choice = ask("  Choose [1/2/3, default 1] (or '?' for help): ",
                 topic="undo") or "1"
    if choice == "1":
        cmd.append("--dry-run")
        return cmd
    if choice == "2":
        cmd.append("--list")
        return cmd
    if choice == "3":
        # --yes here: we already asked them to choose option 3; another
        # confirmation would just be friction. The cardo.py side prints
        # the run summary before doing anything, so they still see what's
        # about to happen.
        if confirm("Also force-overwrite destinations that already exist?",
                   default_no=True):
            cmd.append("--force")
        cmd.append("-y")
        return cmd
    # Anything else: treat as cancel.
    return None


# Maps each menu action to its argv-builder function.
BUILDERS: dict[str, Callable[[list[str]], list[str] | None]] = {
    "stats":      _build_stats,
    "tree":       _build_tree,
    "clean":      _build_clean,
    "search":     _build_search,
    "name-clash": _build_name_clash,
    "organize":   _build_organize,
    "copy":       _build_copy_move,
    "move":       _build_copy_move,
    "rename":     _build_rename,
    "dedupe":     _build_dedupe,
    "undo":       _build_undo,
}


def build_command(action: str) -> list[str] | None:
    """Prompt the user for the args specific to `action` and return argv."""
    builder = BUILDERS.get(action)
    if builder is None:
        return None
    cmd = builder([sys.executable, str(FILEMGR), action])
    if cmd is None:
        return None

    # Common tail logic: write ops get auto-logging; read-only ops get
    # an optional HTML report (unless dedupe already asked).
    if action in WRITING_ACTIONS and "-n" not in cmd:
        cmd.append("--log")
    if action in REPORTABLE_READONLY and "--report" not in cmd:
        if confirm("Save an HTML report of the findings?", topic="report"):
            cmd.append("--report")
    return cmd


# ──────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────

def banner() -> None:
    print("\n" + "═" * 50)
    print("            cardo — file manager")
    print("═" * 50)


def render_menu() -> None:
    banner()
    for i, item in enumerate(MENU, start=1):
        print(f"  {i:>2}. {item.label}")
    print(f"   q. Quit")
    print(f"   ?. Help  (type at any prompt for context-sensitive help)")
    # Status line: warns about missing optional dependencies so the user
    # doesn't get surprised mid-flow by features that aren't available.
    if not _TRASH_AVAILABLE:
        print(f"\n  Note: `send2trash` is not installed — Dedupe/Clean will "
              f"permanently delete.\n  Install with: pip install send2trash")


def get_main_choice() -> str | None:
    """Show the menu and read one valid response. Returns the choice string
    (digit or 'q'-like), or None if the user wants to quit via EOF/Ctrl+C."""
    while True:
        render_menu()
        try:
            choice = ask("\n  Choose an action: ", topic="main_menu").lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice in ("q", "quit", "exit"):
            return "q"
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            return choice
        # Anything else: tell the user and re-render so the menu is back
        # on screen. Empty input just brings the menu back without nagging.
        if choice:
            print("  ↑ Not a valid choice. Type a number, 'q' to quit, or '?' for help.")


def run_cardo(cmd: list[str]) -> None:
    """Run cardo.py as a subprocess and print framing.

    For destructive subcommands, print a one-line heads-up about the
    installation-folder protection so the user isn't surprised when a
    "Proceed?" prompt appears mid-run. The protection itself lives in
    cardo.py — we just set expectations here.
    """
    # cmd shape: [python, cardo.py, subcommand, ...args]
    subcommand = cmd[2] if len(cmd) > 2 else None
    print("\n  Running:", " ".join(shlex.quote(c) for c in cmd[2:]))
    if subcommand in WRITING_ACTIONS and "--include-unsafe" not in cmd:
        print("  (Protection is on: installed-application content will be "
              "skipped with a prompt.)")
    print("─" * 50)
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    print("─" * 50)


def main() -> int:
    if not FILEMGR.exists():
        print(f"✗ Can't find cardo.py next to this launcher.", file=sys.stderr)
        print(f"  Expected at: {FILEMGR}", file=sys.stderr)
        try:
            input("\nPress Enter to close…")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1

    while True:
        choice = get_main_choice()
        if choice is None or choice == "q":
            return 0

        item = MENU[int(choice) - 1]

        if item.action == "_view_logs":
            view_recent_logs()
            try:
                ask("\n  Press Enter to continue… ")
            except (EOFError, KeyboardInterrupt):
                return 0
            continue

        if item.action == "_config":
            try:
                handle_config_menu()
            except (EOFError, KeyboardInterrupt):
                print("\n  Cancelled.")
            try:
                ask("\n  Press Enter to continue… ")
            except (EOFError, KeyboardInterrupt):
                return 0
            continue

        if item.action == "_help":
            try:
                handle_help_browser()
            except (EOFError, KeyboardInterrupt):
                pass
            continue

        try:
            cmd = build_command(item.action)
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            continue
        if cmd is None:
            print("  Cancelled.")
            continue

        run_cardo(cmd)

        try:
            again = ask("\n  Run another command? [Y/n]: ", topic="yesno").lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if again in ("n", "no", "q", "quit"):
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
