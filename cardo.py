#!/usr/bin/env python3
"""
cardo.py — a friendly command-line file manager.

A single-file utility for everyday file wrangling: copy, move, rename in bulk,
deduplicate, organize by type, search, and tidy up empty folders.

Every destructive operation supports --dry-run so you can preview the damage
before committing. Most also support --interactive to confirm per-file.

USAGE
    python cardo.py <command> [options]

COMMANDS
    copy        Copy files/folders, with optional glob filter
    move        Move files/folders, with optional glob filter
    rename      Bulk rename via regex, prefix, suffix, lowercase, or numbering
    dedupe      Find/remove duplicate files by SHA-256 hash
    name-clash  Report files sharing a name across the tree (read-only)
    organize    Sort files into subfolders by extension category
    search      Find files by name, size, age, or extension
    tree        Pretty-print a directory tree with sizes
    clean       Remove empty directories recursively
    stats       Show a size/count breakdown of a directory

Run `python cardo.py <command> --help` for command-specific options.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

# tomllib is stdlib on 3.11+. Older Pythons can still run cardo — they
# just won't be able to load a config file.
try:
    import tomllib  # type: ignore[import-not-found]
    _TOML_AVAILABLE = True
except ImportError:
    tomllib = None  # type: ignore[assignment]
    _TOML_AVAILABLE = False

# send2trash is optional. If absent, --trash flags will warn and refuse;
# permanent-delete behavior is unaffected. Install with `pip install send2trash`.
try:
    from send2trash import send2trash as _send2trash_impl
    _TRASH_AVAILABLE = True
except ImportError:
    _send2trash_impl = None  # type: ignore[assignment]
    _TRASH_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────
# Constants & lightweight helpers
# ──────────────────────────────────────────────────────────────────────────

CATEGORIES: dict[str, set[str]] = {
    "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff", ".ico"},
    "Videos":    {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"},
    "Audio":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".tex", ".md", ".txt", ".epub"},
    "Sheets":    {".xls", ".xlsx", ".ods", ".csv", ".tsv"},
    "Slides":    {".ppt", ".pptx", ".odp", ".key"},
    "Archives":  {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz"},
    "Code":      {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
                  ".hpp", ".rs", ".go", ".rb", ".php", ".sh", ".html", ".css", ".scss",
                  ".sql", ".yaml", ".yml", ".json", ".toml", ".xml"},
    "Fonts":     {".ttf", ".otf", ".woff", ".woff2"},
    "Installers":{".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk"},
}

# Flat reverse lookup, rebuilt whenever CATEGORIES is extended via config.
# Code paths that classify by extension go through category_for(), which
# reads _EXT_TO_CATEGORY — so updating this dict in place is enough.
_EXT_TO_CATEGORY: dict[str, str] = {}


def _rebuild_ext_to_category() -> None:
    """(Re)build _EXT_TO_CATEGORY from CATEGORIES. Call after merging config."""
    _EXT_TO_CATEGORY.clear()
    for name, exts in CATEGORIES.items():
        for ext in exts:
            _EXT_TO_CATEGORY[ext.lower()] = name


_rebuild_ext_to_category()


def human_size(n: int | float) -> str:
    """1234567 -> '1.2 MB'."""
    if n < 1024:
        return f"{int(n)} B"
    val = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        val /= 1024
        if val < 1024:
            return f"{val:.1f} {unit}"
    return f"{val:.1f} PB"


def category_for(ext: str) -> str:
    return _EXT_TO_CATEGORY.get(ext.lower(), "Other")


def iter_files(root: Path, recursive: bool, pattern: str | None = None) -> Iterator[Path]:
    """Yield files under root, optionally filtered by a glob pattern."""
    if recursive:
        walker: Iterator[Path] = (p for p in root.rglob("*") if p.is_file())
    else:
        walker = (p for p in root.iterdir() if p.is_file())
    if pattern:
        return (p for p in walker if fnmatch.fnmatch(p.name, pattern))
    return walker


def safe_stat(p: Path) -> os.stat_result | None:
    """Stat a path, returning None on permission/IO error instead of raising."""
    try:
        return p.stat()
    except OSError:
        return None


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def unique_path(dest: Path) -> Path:
    """If dest exists, append ' (1)', ' (2)', ... before the suffix."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _send_to_trash(path: Path) -> tuple[bool, str | None]:
    """Move `path` to the OS trash. Returns (success, error_message).

    On macOS: uses Finder's Trash. On Linux (freedesktop): uses ~/.local/share/Trash
    or the per-volume trash. On Windows: uses the Recycle Bin.

    If `send2trash` isn't importable, returns (False, "send2trash not installed").
    Callers are expected to honor that by NOT falling back to unlink — the user
    asked for trash specifically.
    """
    if not _TRASH_AVAILABLE:
        return (False, "send2trash not installed (pip install send2trash)")
    try:
        # send2trash accepts str or Path; passing str is the lowest-common-
        # denominator across versions.
        _send2trash_impl(str(path))
        return (True, None)
    except OSError as e:
        # send2trash raises OSError subclasses on permission / unsupported-fs
        # cases. The message is usually informative enough to surface as-is.
        return (False, str(e))
    except Exception as e:  # noqa: BLE001 — third-party library, defensive
        return (False, f"{type(e).__name__}: {e}")


def trash_or_warn_if_requested(args) -> tuple[bool, str | None]:
    """Decide whether the user asked for trash AND whether we can honor it.

    Returns (use_trash, error_to_print). If error_to_print is not None, the
    caller should print it and abort the operation — we never silently
    fall back to permanent delete when --trash was requested.
    """
    use_trash = bool(getattr(args, "trash", False))
    if not use_trash:
        return (False, None)
    if not _TRASH_AVAILABLE:
        return (False,
                "✗ --trash was requested but `send2trash` is not installed.\n"
                "  Install it with:  pip install send2trash\n"
                "  Or omit --trash to permanently delete instead.")
    return (True, None)


# ──────────────────────────────────────────────────────────────────────────
# Config file (~/.cardo/config.toml)
#
# Precedence: explicit CLI flag > config file > built-in default.
#
# Layout:
#   [defaults]              # applied to every command
#     assume_yes = false    # skip the "Proceed?" prompt (-y)
#     report     = false    # auto-save HTML reports (--report)
#     log        = true     # auto-log write operations (--log)
#
#   [dedupe]                # cmd_dedupe specifics
#     mode    = "standard"  # quick / standard / paranoid
#     min_size_kb = 4
#     workers = 0           # 0 = auto
#
#   [categories]            # extend or override organize() buckets
#     Notebooks = [".ipynb", ".rmd"]
#     Raw       = [".cr2", ".nef", ".arw", ".dng"]
#
# Custom categories MERGE with the built-ins: extensions you list are
# moved out of "Other" and into your chosen bucket. To replace a built-in
# bucket entirely, give it a name that matches a built-in (e.g. "Images")
# and your list wins for any extensions you mention.
# ──────────────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".cardo"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    """Loaded user configuration. All fields have safe built-in defaults.

    The Config instance is built once in main() and consulted as the CLI
    flags are processed: see _apply_config_defaults().
    """
    # [defaults]
    assume_yes: bool = False
    report:     bool = False
    log:        bool = False
    trash:      bool = False

    # [dedupe]
    dedupe_mode:        str = "standard"
    dedupe_min_size_kb: int = 4
    dedupe_workers:     int = 0

    # [categories] — merged into the global CATEGORIES at load time.
    extra_categories: dict[str, list[str]] = field(default_factory=dict)

    # Non-default values are tracked so we can show the user which settings
    # came from their config file (useful for `cardo config show`).
    source_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


# The single live config used by the rest of the program. Replaced in main()
# once we've read the file (if any).
CONFIG: Config = Config()


def _coerce(value: Any, kind: type, key: str) -> Any:
    """Light type-checking for config values. Bad types print a warning and
    fall back to the default rather than crashing."""
    if isinstance(value, kind):
        return value
    print(f"  ! config: {key} should be {kind.__name__}, got "
          f"{type(value).__name__} ({value!r}) — ignoring.", file=sys.stderr)
    return None


def load_config(path: Path = CONFIG_FILE) -> Config:
    """Load `path` if it exists, else return a default Config.

    Errors (missing tomllib, unreadable file, bad TOML) print to stderr but
    never raise — we always return a usable Config.
    """
    cfg = Config()
    if not path.exists():
        return cfg
    if not _TOML_AVAILABLE:
        print(f"  ! config: {path} exists but Python {sys.version_info.major}."
              f"{sys.version_info.minor} has no tomllib (needs 3.11+). Ignoring.",
              file=sys.stderr)
        return cfg
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        print(f"  ! config: could not read {path}: {e}", file=sys.stderr)
        return cfg
    except tomllib.TOMLDecodeError as e:
        print(f"  ! config: invalid TOML in {path}: {e}", file=sys.stderr)
        return cfg

    cfg.source_path = path

    # [defaults]
    defaults = data.get("defaults", {}) or {}
    for key, attr, kind in (
        ("assume_yes", "assume_yes", bool),
        ("report",     "report",     bool),
        ("log",        "log",        bool),
        ("trash",      "trash",      bool),
    ):
        if key in defaults:
            coerced = _coerce(defaults[key], kind, f"defaults.{key}")
            if coerced is not None:
                setattr(cfg, attr, coerced)
                cfg.overrides[f"defaults.{key}"] = coerced

    # [dedupe]
    dedupe = data.get("dedupe", {}) or {}
    if "mode" in dedupe:
        val = _coerce(dedupe["mode"], str, "dedupe.mode")
        if val in ("quick", "standard", "paranoid"):
            cfg.dedupe_mode = val
            cfg.overrides["dedupe.mode"] = val
        elif val is not None:
            print(f"  ! config: dedupe.mode must be quick/standard/paranoid "
                  f"(got {val!r}) — ignoring.", file=sys.stderr)
    if "min_size_kb" in dedupe:
        val = _coerce(dedupe["min_size_kb"], int, "dedupe.min_size_kb")
        if val is not None and val >= 0:
            cfg.dedupe_min_size_kb = val
            cfg.overrides["dedupe.min_size_kb"] = val
    if "workers" in dedupe:
        val = _coerce(dedupe["workers"], int, "dedupe.workers")
        if val is not None and val >= 0:
            cfg.dedupe_workers = val
            cfg.overrides["dedupe.workers"] = val

    # [categories] — values must be lists of strings
    cats = data.get("categories", {}) or {}
    for name, exts in cats.items():
        if not isinstance(exts, list) or not all(isinstance(e, str) for e in exts):
            print(f"  ! config: categories.{name} must be a list of strings — "
                  f"ignoring.", file=sys.stderr)
            continue
        # Normalize: lowercase, leading dot
        normalized = []
        for e in exts:
            e = e.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            normalized.append(e)
        if normalized:
            cfg.extra_categories[name] = normalized
            cfg.overrides[f"categories.{name}"] = normalized

    return cfg


def apply_config_to_globals(cfg: Config) -> None:
    """Merge config-derived state into module-level globals.

    Today this means folding cfg.extra_categories into CATEGORIES (and
    rebuilding the lookup). Kept as a separate function so future
    additions (throughput overrides, etc.) have an obvious home.
    """
    if not cfg.extra_categories:
        return
    for name, exts in cfg.extra_categories.items():
        bucket = CATEGORIES.setdefault(name, set())
        bucket.update(exts)
    _rebuild_ext_to_category()


def _apply_config_defaults(args: argparse.Namespace, cfg: Config) -> None:
    """Fill in argparse defaults from the config when the CLI didn't set them.

    argparse gives us a known sentinel for each flag (False/None/etc); we
    only override when the user *didn't* pass the flag. This preserves
    the precedence rule: explicit CLI > config > built-in default.
    """
    cmd = getattr(args, "command", None)

    # [defaults] apply to every command that has the matching attribute.
    if cfg.assume_yes and getattr(args, "yes", False) is False:
        args.yes = True
    if cfg.report and getattr(args, "report", False) is False \
            and hasattr(args, "report"):
        args.report = True
    if cfg.log and getattr(args, "log", None) is None \
            and hasattr(args, "log"):
        # Empty string is argparse's "--log with no value" sentinel.
        args.log = ""
    if cfg.trash and getattr(args, "trash", False) is False \
            and hasattr(args, "trash"):
        args.trash = True

    # [dedupe] section only matters for the dedupe command.
    if cmd == "dedupe":
        # argparse default for --mode is "standard", so we can only tell
        # the user *didn't* set it by comparing to the parser default. We
        # store that default on the args namespace via a side channel:
        # see build_parser(). For now, override unconditionally if the
        # config explicitly set it.
        if "dedupe.mode" in cfg.overrides:
            # Only override if user left it at parser default.
            if getattr(args, "_cli_mode_explicit", False) is False:
                args.mode = cfg.dedupe_mode
        if "dedupe.min_size_kb" in cfg.overrides:
            if getattr(args, "_cli_min_size_explicit", False) is False:
                args.min_size = cfg.dedupe_min_size_kb
        if "dedupe.workers" in cfg.overrides:
            if getattr(args, "workers", 0) == 0:
                args.workers = cfg.dedupe_workers


def _config_init_text() -> str:
    """Starter file content written by `cardo config init`.

    Every key is commented out — the file documents what's available and
    its built-in default. Uncomment a line to make it take effect.
    """
    return """\
# cardo configuration
# All sections optional. Anything you omit keeps its built-in default.
# CLI flags always win over values set here.
#
# Uncomment any line below to make that setting take effect.

[defaults]
# Skip the "Proceed?" prompt that asks before long-running operations.
# At your own risk — operations will start without confirming.
# assume_yes = false

# Automatically save an HTML report for commands that support --report.
# report = false

# Automatically write a log file for write operations (copy, move,
# rename, dedupe, organize, clean). Logs land in ~/.cardo/logs/.
# log = false

# Send deleted files to the OS trash instead of unlinking them permanently.
# Applies to `dedupe` (duplicate deletion) and `clean` (empty directories).
# Requires the `send2trash` package: pip install send2trash
# trash = false

[dedupe]
# Default scan mode. One of: "quick", "standard", "paranoid".
# mode = "standard"

# Skip files smaller than this many KB.
# min_size_kb = 4

# Parallel hashing workers. 0 = auto (min(8, CPU count)).
# Set to 1 to disable threading entirely.
# workers = 0

[categories]
# Extend the file-type buckets used by `organize` and `stats`. Each entry
# is a category name → list of extensions (with or without leading dot).
# These merge with the built-in categories.
#
# Examples (uncomment and edit):
# Notebooks = [".ipynb", ".rmd"]
# Raw       = [".cr2", ".nef", ".arw", ".dng"]
# ThreeD    = [".obj", ".fbx", ".gltf", ".usd", ".usdz", ".blend"]
"""


def cmd_config(args) -> int:
    """`cardo config {show,path,init}`"""
    action = args.config_action
    if action == "path":
        print(CONFIG_FILE)
        return 0

    if action == "init":
        if CONFIG_FILE.exists() and not args.force:
            print(f"  Config file already exists: {CONFIG_FILE}", file=sys.stderr)
            print(f"  Use --force to overwrite.", file=sys.stderr)
            return 1
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_config_init_text(), encoding="utf-8")
        except OSError as e:
            print(f"✗ Could not write config: {e}", file=sys.stderr)
            return 1
        print(f"  Wrote starter config to {CONFIG_FILE}")
        print(f"  Edit it in any text editor; all keys are optional.")
        return 0

    if action == "show":
        print(f"  Config file:    {CONFIG_FILE}")
        print(f"  File present:   {'yes' if CONFIG_FILE.exists() else 'no'}")
        print(f"  TOML available: {'yes' if _TOML_AVAILABLE else 'no (needs Python 3.11+)'}")
        print(f"  Trash support:  {'yes' if _TRASH_AVAILABLE else 'no (pip install send2trash)'}")
        print()
        print("  Effective settings:")
        print(f"    defaults.assume_yes   = {CONFIG.assume_yes}")
        print(f"    defaults.report       = {CONFIG.report}")
        print(f"    defaults.log          = {CONFIG.log}")
        print(f"    defaults.trash        = {CONFIG.trash}")
        print(f"    dedupe.mode           = {CONFIG.dedupe_mode!r}")
        print(f"    dedupe.min_size_kb    = {CONFIG.dedupe_min_size_kb}")
        print(f"    dedupe.workers        = {CONFIG.dedupe_workers}")
        if CONFIG.extra_categories:
            print("    categories (added):")
            for name, exts in sorted(CONFIG.extra_categories.items()):
                print(f"      {name}: {', '.join(exts)}")
        if CONFIG.overrides:
            print()
            print("  Values overridden by config file:")
            for key, val in sorted(CONFIG.overrides.items()):
                print(f"    {key} = {val!r}")
        else:
            print()
            print("  (No overrides — using all built-in defaults.)")
        return 0

    print(f"✗ Unknown config action: {action}", file=sys.stderr)
    return 2


# ──────────────────────────────────────────────────────────────────────────
# Unsafe-path detection: never dedupe inside packages, app bundles, etc.
# ──────────────────────────────────────────────────────────────────────────

# Path segments that indicate "this file is inside something the OS or an
# application manages — never delete byte-duplicates from inside these."
UNSAFE_PATH_SEGMENTS = (
    # macOS app/framework/bundle internals
    ".app", ".framework", ".bundle", ".kext", ".plugin", ".xpc",
    ".lproj",  # localization bundles inside apps
    # Lightroom catalog & preview data
    ".lrcat-data", ".lrdata", ".lrlibrary",
    # Photos library
    ".photoslibrary",
)

# Big files (installers, disk images) — duplicates indicate "you have the same
# installer in two places". Reported but not auto-deleted.
ADVISORY_EXTENSIONS = {
    ".pkg", ".mpkg",   # macOS installer packages
    ".dmg",            # disk images
    ".iso",            # disc images
    ".exe", ".msi",    # Windows installers
    ".deb", ".rpm",    # Linux packages
    ".apk", ".ipa",    # mobile app packages
}

# Specific filenames that look identical by content but each belongs to its
# own context.
UNSAFE_FILENAMES = {
    ".DS_Store",          # macOS Finder metadata, one per folder
    "LOCK",               # Lightroom catalog lock files
    "Thumbs.db",          # Windows thumbnail cache
    ".localized",         # macOS localization marker
    "desktop.ini",        # Windows folder metadata
}

# Filename suffixes/patterns that are unsafe.
UNSAFE_FILENAME_PATTERNS = (
    ".log",               # transaction logs, debug logs — keep them in place
    ".lock",              # generic lock files
)

# ─── Installation-folder detection ─────────────────────────────────────────
# Some applications install as plain folders rather than .app bundles (Adobe,
# Maxon C4D, JetBrains, Unity, etc). Detected by combining several signals.

INSTALL_FOLDER_CHILD_HINTS = {
    "presets", "resources", "resource", "plug-ins", "plugins", "frameworks",
    "library", "libraries", "lib", "bin", "share", "locale",
    "help", "documentation", "configuration", "components",
    "exchange plugins", "scripts", "templates", "modules",
    "support files", "supportfiles",
}

INSTALL_FOLDER_FILE_HINTS = {
    "license.txt", "license.rtf", "license.md", "license",
    "notice.txt", "notice.rtf", "notice",
    "readme.txt", "readme.rtf", "readme",
    "version.txt", "version", ".version",
    "info.plist", "third_party_notices.txt", "third-party-notices.txt",
    "uninstall.sh", "uninstaller", "uninstall.exe",
    "install.log", "installation.log",
    "eula.txt", "eula.rtf", "eula",
}

INSTALL_FOLDER_BINARY_EXTS = {
    ".dylib", ".so", ".dll",       # native shared libraries
    ".jar",                         # Java archives
    ".pak", ".pack",                # binary asset bundles
    ".node",                        # Node native modules
    ".framework",                   # macOS framework folders
    ".plist",                       # macOS property lists
}

INSTALL_FOLDER_REVERSE_DNS_PREFIXES = (
    "com.", "net.", "org.", "io.", "co.", "uk.", "de.", "fr.",
)

_VENDOR_PREFIXES = (
    "adobe ", "maxon ", "autodesk ", "microsoft ", "apple ",
    "blackmagic ", "avid ", "steinberg ", "ableton ", "native instruments ",
    "jetbrains ", "unity ", "unreal ", "houdini", "blender ", "redshift ",
    "marvelous designer ", "zbrush",
)
_VENDOR_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_VENDOR_VERSION_RE = re.compile(r"\b(v?\d+(\.\d+)+)\b")


def _has_vendor_like_name(folder_name: str) -> bool:
    """Does this folder name look like 'AppName YYYY' or 'Vendor AppName N.N'?"""
    lower = folder_name.lower()
    if any(lower.startswith(v) for v in _VENDOR_PREFIXES):
        return True
    if _VENDOR_YEAR_RE.search(folder_name):
        return True
    if _VENDOR_VERSION_RE.search(folder_name):
        return True
    return False


def _safe_iterdir(folder: Path) -> list[Path]:
    """iterdir() that returns [] on any OS error rather than raising."""
    try:
        return list(folder.iterdir())
    except (OSError, PermissionError):
        return []


def looks_like_install_folder(folder: Path, max_probe: int = 200) -> tuple[bool, list[str]]:
    """Heuristically decide whether `folder` is an installed app/SDK root.

    Returns (is_install, evidence) so the caller can show the user *why*.
    Signals (any 2 → install; 1 + vendor-like name → install):
      - app-style child folder names (Presets/Resources/Frameworks/lib)
      - vendor marker files (LICENSE, version.txt, Info.plist)
      - reverse-DNS-named entries (com.adobe.*)
      - high concentration of binary library files (.dylib/.so/.dll)
    """
    evidence: list[str] = []
    children = _safe_iterdir(folder)[:max_probe]
    if not children:
        return (False, [])

    # Signal 1: telltale child folder names
    folder_hits = [c.name for c in children
                   if c.is_dir() and c.name.lower() in INSTALL_FOLDER_CHILD_HINTS]
    if folder_hits:
        names = ", ".join(folder_hits[:3])
        more = f" + {len(folder_hits) - 3} more" if len(folder_hits) > 3 else ""
        evidence.append(f"contains app-style subfolders: {names}{more}")

    # Signal 2: vendor marker files
    file_hits = [c.name for c in children
                 if c.is_file() and c.name.lower() in INSTALL_FOLDER_FILE_HINTS]
    if file_hits:
        evidence.append(f"vendor files present: {', '.join(file_hits[:3])}")

    # Signal 3: reverse-DNS-named entries in immediate subtree
    if _has_reverse_dns(folder):
        evidence.append("contains reverse-DNS named items (com.*, net.*, etc)")

    # Signal 4: binary library files in folder or one level down
    binary_count = _count_binaries(children, threshold=5)
    if binary_count >= 5:
        evidence.append(
            f"contains {binary_count}+ binary library files (.dylib/.so/.dll)"
        )

    if len(evidence) >= 2:
        return (True, evidence)
    if evidence and _has_vendor_like_name(folder.name):
        evidence.append(f"folder name '{folder.name}' looks like a versioned product")
        return (True, evidence)
    return (False, [])


def _has_reverse_dns(folder: Path, max_depth: int = 3, needed: int = 2) -> bool:
    """Two-level scan: looking for child names beginning com.*, net.* etc."""
    hits = 0

    def scan(p: Path, depth: int) -> bool:
        nonlocal hits
        if depth > max_depth:
            return False
        for c in _safe_iterdir(p)[:60]:
            name_lower = c.name.lower()
            if any(name_lower.startswith(pref) for pref in INSTALL_FOLDER_REVERSE_DNS_PREFIXES):
                # Must look like com.foo.bar, not just com.something or .com
                tail = name_lower.split(".", 1)[1] if "." in name_lower else ""
                if "." in tail:
                    hits += 1
                    if hits >= needed:
                        return True
            if c.is_dir() and scan(c, depth + 1):
                return True
        return False

    return scan(folder, 0)


def _count_binaries(children: list[Path], threshold: int) -> int:
    """Count .dylib/.so/.dll files at this level and one below, stopping at threshold."""
    count = 0
    for c in children:
        if c.is_file() and c.suffix.lower() in INSTALL_FOLDER_BINARY_EXTS:
            count += 1
            if count >= threshold:
                return count
        elif c.is_dir():
            for sub in _safe_iterdir(c)[:100]:
                if sub.is_file() and sub.suffix.lower() in INSTALL_FOLDER_BINARY_EXTS:
                    count += 1
                    if count >= threshold:
                        return count
    return count


def find_install_folders(root: Path, recursive: bool, max_depth: int = 3) -> dict[Path, list[str]]:
    """Walk `root` looking for installation folders.

    Returns a dict mapping each install-folder path to the evidence found.
    Stops descending into a folder once it's identified as an install root.
    """
    found: dict[Path, list[str]] = {}

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth:
            return
        is_install, evidence = looks_like_install_folder(folder)
        if is_install:
            found[folder] = evidence
            return  # don't descend; the whole subtree is protected
        if not recursive:
            return
        for child in _safe_iterdir(folder):
            if child.is_dir():
                walk(child, depth + 1)

    # Don't classify `root` itself as an install folder — the user explicitly
    # pointed dedupe at it. Just check its children.
    for child in _safe_iterdir(root):
        if child.is_dir():
            walk(child, 1)
    return found


def classify_file(path: Path, install_folders: set[Path] | None = None) -> tuple[str, str | None]:
    """Decide how dedupe should treat this file.

    Returns one of:
      ("normal", None)        — eligible for deletion as a duplicate
      ("blocked", reason)     — never eligible (system file, app bundle, etc)
      ("advisory", reason)    — report duplicates but don't auto-delete
    """
    # Inside a detected installation folder → blocked.
    if install_folders:
        for inst in install_folders:
            try:
                path.relative_to(inst)
                return ("blocked", f"inside installation folder '{inst.name}'")
            except ValueError:
                continue

    # Path segments that mean "inside a package".
    for part in path.parts:
        lower = part.lower()
        for seg in UNSAFE_PATH_SEGMENTS:
            if lower.endswith(seg):
                return ("blocked", f"inside {seg} package")

    if path.name in UNSAFE_FILENAMES:
        return ("blocked", f"system file ({path.name})")
    name_lower = path.name.lower()
    for suffix in UNSAFE_FILENAME_PATTERNS:
        if name_lower.endswith(suffix):
            return ("blocked", f"managed file ({suffix})")
    if path.suffix.lower() in ADVISORY_EXTENSIONS:
        return ("advisory", f"installer/package ({path.suffix.lower()})")
    return ("normal", None)


# Kept for backwards compatibility.
def is_unsafe_to_dedupe(path: Path) -> str | None:
    cls, reason = classify_file(path)
    return reason if cls == "blocked" else None


# ──────────────────────────────────────────────────────────────────────────
# Shared protection preflight
#
# Used by every destructive command (clean, organize, move, rename) to avoid
# touching the guts of installed applications. The mechanism predates this
# section in `dedupe`, but only dedupe used to consult it — leading to incidents
# where `clean` happily trashed `Adobe InDesign 2026/Presets/...`, `Cinema 4D
# 2024/resource/modules/...` and so on.
#
# Two layers of protection:
#
#   1. Path-segment check — any path containing a segment ending in .app,
#      .framework, .lrdata etc is treated as inside a managed package.
#   2. Install-folder detection — scans the tree once with the existing
#      `find_install_folders()` and protects anything underneath a detected
#      installation root (Adobe / Maxon / JetBrains-style folders).
#
# Each destructive command calls `partition_safe_protected()` after building
# its plan, prints a friendly summary of what would be skipped, and asks for
# one confirmation. `--include-unsafe` opts out entirely (matches dedupe).
# ──────────────────────────────────────────────────────────────────────────

def is_protected_path(path: Path, install_folders: set[Path]) -> tuple[bool, str | None]:
    """Return (is_protected, reason) for `path`.

    `install_folders` is the set returned by find_install_folders() for the
    tree being operated on. The caller computes it once per run and passes
    it through — calling find_install_folders() per file would be O(n²).

    Note: this handles both files AND directories. For dedupe we used
    classify_file() which is file-oriented; for clean/organize/move/rename
    we need the directory-aware variant.
    """
    # Inside a detected installation folder?
    if install_folders:
        for inst in install_folders:
            try:
                path.relative_to(inst)
                return (True, f"inside installation folder '{inst.name}'")
            except ValueError:
                continue

    # Inside a managed package (anywhere in the path)?
    for part in path.parts:
        lower = part.lower()
        for seg in UNSAFE_PATH_SEGMENTS:
            if lower.endswith(seg):
                return (True, f"inside {seg} package")

    # Specific never-touch system filenames (these only apply to files,
    # but checking is cheap).
    if path.name in UNSAFE_FILENAMES:
        return (True, f"system file ({path.name})")

    return (False, None)


def detect_install_folders_with_root_check(root: Path, recursive: bool) -> tuple[set[Path], list[str]]:
    """Wrapper around find_install_folders() that also checks whether `root`
    itself looks like an install folder.

    Returns (install_folders, root_warnings). The first is the set of detected
    install folders — INCLUDING `root` itself if it qualifies, so a per-file
    `is_protected_path()` check correctly fires for everything under it. The
    second is a list of human-readable warning lines about the root itself.
    """
    install = set(find_install_folders(root, recursive).keys())
    warnings: list[str] = []

    # find_install_folders() deliberately doesn't classify the root itself —
    # that's the right call for `dedupe` (the user explicitly pointed dedupe at
    # the root). But for destructive ops, if the user just pointed `move` at
    # an Adobe folder, every file under it should still be protected. So we
    # ALSO classify the root and, if it looks like an install, add it to the
    # set so per-file checks fire.
    is_install, evidence = looks_like_install_folder(root)
    if is_install:
        install.add(root)
        warnings.append(
            f"the folder you specified ({root}) itself looks like an installed "
            f"application:"
        )
        for ev in evidence:
            warnings.append(f"  — {ev}")

    return install, warnings


def partition_safe_protected(
    plan: Iterable[Path | tuple],
    install_folders: set[Path],
    *,
    path_of: Callable[[Any], Path] | None = None,
) -> tuple[list[Any], list[tuple[Any, str]]]:
    """Split a plan into (safe, protected_with_reason).

    `plan` can be any iterable. Each entry may be a Path directly, or any
    structure from which `path_of(entry)` extracts the Path to check. This
    lets the same helper serve clean (which plans bare directory paths),
    rename (which plans (old, new) tuples), and move/organize (which plan
    (src, dst, size) tuples).

    For rename and move, we check the *source* path: that's what's being
    mutated. Checking the destination is the caller's job if relevant
    (the move command does both source and destination-parent).
    """
    if path_of is None:
        path_of = lambda x: x  # type: ignore[assignment]

    safe: list[Any] = []
    protected: list[tuple[Any, str]] = []
    for entry in plan:
        p = path_of(entry)
        is_prot, reason = is_protected_path(p, install_folders)
        if is_prot:
            protected.append((entry, reason or "protected"))
        else:
            safe.append(entry)
    return safe, protected


def confirm_protection_skip(
    command: str,
    safe_count: int,
    protected: list[tuple[Any, str]],
    *,
    path_of: Callable[[Any], Path] | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    root_warnings: list[str] | None = None,
) -> bool:
    """Show the user how many actions were skipped for safety and ask once.

    Returns True if the caller should proceed with the `safe_count` actions.
    Returns False if the user declined (or there's literally nothing to do).

    `assume_yes` and `dry_run` skip the confirmation — dry-run doesn't
    change anything anyway, and -y is the existing escape hatch for the
    confirmation prompt.
    """
    if path_of is None:
        path_of = lambda x: x  # type: ignore[assignment]

    # Show root-level warnings first (if we're operating ON an install folder)
    if root_warnings:
        print()
        for line in root_warnings:
            print(f"  ⚠ {line}")

    if not protected:
        return True

    # Group reasons by category to keep the summary compact.
    by_reason: dict[str, int] = defaultdict(int)
    for _, reason in protected:
        by_reason[reason] += 1

    print()
    print(f"  ⚠ Protection: skipping {len(protected):,} action(s) that would "
          f"touch installed-application content:")
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1])[:6]:
        print(f"      {count:>6,}× {reason}")
    if len(by_reason) > 6:
        print(f"      … and {len(by_reason) - 6} other categor"
              f"{'ies' if len(by_reason) - 6 > 1 else 'y'}")

    # Show a handful of example paths so the user can verify the protection
    # is doing the right thing, without flooding the terminal.
    sample = min(5, len(protected))
    print(f"\n  First {sample} of {len(protected):,}:")
    for entry, reason in protected[:sample]:
        print(f"      • {path_of(entry)}")
    if len(protected) > sample:
        print(f"      … and {len(protected) - sample:,} more")
    print()
    print(f"  Pass --include-unsafe to override this protection (not recommended).")
    print()

    if safe_count == 0:
        print(f"  Nothing safe to do — every planned action was protected. Aborting.")
        return False

    print(f"  {command} will proceed with {safe_count:,} safe action(s) and "
          f"skip the {len(protected):,} protected one(s).")

    if dry_run or assume_yes:
        return True
    try:
        ans = input("  Proceed? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans in ("y", "yes"):
        return True
    print("Aborted.")
    return False


# ──────────────────────────────────────────────────────────────────────────
# Hashing
# ──────────────────────────────────────────────────────────────────────────

_HASH_CHUNK = 1 << 20  # 1 MiB


def file_hash(path: Path, chunk: int = _HASH_CHUNK) -> str:
    """SHA-256 of a file, streamed in chunks."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def file_hash_prefix(path: Path, prefix_bytes: int = 65536) -> str:
    """SHA-256 of just the first `prefix_bytes` of a file.

    Same-sized files that aren't duplicates almost always differ within their
    first few KB, so this lets us cheaply eliminate 80-95% of candidates
    before paying the cost of a full hash.
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            h.update(f.read(prefix_bytes))
    except OSError:
        return ""
    return h.hexdigest()


def files_are_identical(a: Path, b: Path, chunk: int = _HASH_CHUNK) -> bool:
    """Byte-by-byte comparison of two files. Stops at first difference."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with a.open("rb") as fa, b.open("rb") as fb:
            while True:
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:  # both EOF
                    return True
    except OSError:
        return False  # treat unreadable as "not safe to assume identical"


# ──────────────────────────────────────────────────────────────────────────
# Hash cache — speeds up repeat scans by remembering previous results
# ──────────────────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cardo" / "cache"
CACHE_FILE = CACHE_DIR / "hashes.json"


class HashCache:
    """Persistent cache: (resolved_path, size, mtime) → sha256.

    Plain JSON on disk. An entry is invalidated whenever size or mtime
    changes, so a touched/edited file is correctly re-hashed.

    Thread-safe: get/put/stats all take `_lock`. Concurrent dedupe workers
    can hammer this freely.
    """

    def __init__(self, path: Path = CACHE_FILE):
        self.path = path
        self.data: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        # Called from __init__ only, no other thread can see us yet.
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.data = {}  # corrupt cache → start fresh

    def get(self, path: Path) -> str | None:
        st = safe_stat(path)
        if st is None:
            return None
        with self._lock:
            entry = self.data.get(str(path))
            if entry is None:
                self.misses += 1
                return None
            if entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime:
                self.hits += 1
                return entry.get("sha256")
            self.misses += 1
            return None

    def put(self, path: Path, sha256: str) -> None:
        st = safe_stat(path)
        if st is None:
            return
        with self._lock:
            self.data[str(path)] = {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "sha256": sha256,
            }

    def save(self, prune: bool = True) -> None:
        """Write cache to disk, optionally dropping entries for missing files.

        Called after all workers have finished — no lock contention concern in
        practice — but we still take the lock so the dict isn't mutated
        underneath us.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                if prune:
                    self.data = {k: v for k, v in self.data.items() if Path(k).exists()}
                snapshot = dict(self.data)
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f)
        except OSError as e:
            sys.stderr.write(f"\n  ! Could not save hash cache: {e}\n")

    def stats(self) -> str:
        with self._lock:
            hits, misses = self.hits, self.misses
        total = hits + misses
        if total == 0:
            return "no lookups"
        pct = (hits / total) * 100
        return f"{hits:,} cache hits / {total:,} lookups ({pct:.0f}%)"


def cached_file_hash(path: Path, cache: HashCache | None) -> str:
    """SHA-256 of a file, consulting the hash cache when available."""
    if cache is not None:
        cached = cache.get(path)
        if cached is not None:
            return cached
    h = file_hash(path)
    if cache is not None and h:
        cache.put(path, h)
    return h


# ──────────────────────────────────────────────────────────────────────────
# Privilege / admin detection
# ──────────────────────────────────────────────────────────────────────────

# Commands that *might* need elevated privileges depending on what they touch.
NEEDS_PRIVILEGE_CHECK = {"copy", "move", "rename", "dedupe", "organize", "clean"}


def is_admin() -> bool:
    """True if the current process is running as root / Administrator."""
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False


def path_needs_admin(path: Path) -> bool:
    """Does writing to `path` likely require admin rights?

    True when the path (or its nearest existing ancestor) is owned by a
    different user and isn't world-writable. Catches /System, /usr, /Library,
    other users' homes — without false-firing on the current user's own files.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    st = safe_stat(probe)
    if st is None:
        return False
    try:
        current_uid = os.geteuid()  # type: ignore[attr-defined]
    except AttributeError:
        return False  # Windows: different model, skip
    if st.st_uid == current_uid:
        return False
    world_writable = bool(st.st_mode & 0o002)
    return not world_writable


def require_admin_if_needed(command: str, paths: list[Path]) -> bool:
    """Print a clear sudo-required message if needed and return False; else True."""
    if command not in NEEDS_PRIVILEGE_CHECK or is_admin():
        return True
    risky = [p for p in paths if path_needs_admin(p)]
    if not risky:
        return True

    print("✗ This operation needs administrator privileges.", file=sys.stderr)
    print("  The following path(s) are owned by another user and not world-writable:",
          file=sys.stderr)
    for p in risky:
        print(f"    {p}", file=sys.stderr)
    invocation = " ".join(sys.argv)
    print(f"\n  Please re-run with sudo:\n    sudo {invocation}", file=sys.stderr)
    return False


# ──────────────────────────────────────────────────────────────────────────
# Run summary + logging + undo log
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_LOG_DIR = Path.home() / ".cardo" / "logs"
UNDO_DIR = Path.home() / ".cardo" / "undo"

# Operations whose actions are reversible via UndoLog. dedupe is reversible
# only when --trash is used (and even then via the OS trash, not via cardo);
# we deliberately don't log dedupe entries — see cmd_undo below.
UNDOABLE_COMMANDS = {"move", "rename", "organize", "clean"}


class UndoLog:
    """JSONL-formatted machine-readable log of reversible actions.

    One file per run: ~/.cardo/undo/{timestamp}_{command}.jsonl
    Coexists with the human-readable log written by RunSummary; they record
    overlapping but not identical information.

    Format: first line is a `_meta` header, subsequent lines are individual
    action entries. After a successful `cardo undo` run, the meta header's
    `undone` flag is flipped so the next undo skips this file.
    """

    def __init__(self, command: str, argv: list[str]):
        self.command = command
        self.argv = argv
        self.started = time.strftime("%Y-%m-%d %H:%M:%S")
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.path = UNDO_DIR / f"{ts}_{command}.jsonl"
        self.entries: list[dict] = []
        self._file = None
        try:
            UNDO_DIR.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("w", encoding="utf-8")
            # Header line so the file is self-describing without parsing
            # every entry.
            self._write_line({
                "_meta": True,
                "command": command,
                "argv": argv,
                "started": self.started,
                "completed": None,
                "undone": False,
            })
        except OSError as e:
            print(f"  ! Could not open undo log: {e}", file=sys.stderr)
            self._file = None

    def _write_line(self, obj: dict) -> None:
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(obj) + "\n")
            self._file.flush()
        except OSError:
            pass  # undo log failures should never kill the run

    def record(self, op: str, **fields: Any) -> None:
        """Append one undoable action entry. `op` is 'move' / 'rename' / 'rmdir'."""
        if self._file is None:
            return
        entry = {"op": op, "ts": time.time(), **fields}
        self.entries.append(entry)
        self._write_line(entry)

    def close(self, *, completed_ok: bool) -> None:
        """Finalize the log. If no actions were recorded, delete the file —
        no point keeping empty undo logs around to clutter `undo --list`."""
        if self._file is None:
            return
        try:
            self._file.close()
        except OSError:
            pass
        self._file = None
        if not self.entries:
            # Empty undo log — drop it.
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        # Rewrite header in place to update `completed`. JSONL doesn't
        # support seek-and-overwrite cleanly across line lengths, so we
        # read all lines, edit line 0, write back. Cheap for typical run sizes.
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if lines:
                header = json.loads(lines[0])
                header["completed"] = time.strftime("%Y-%m-%d %H:%M:%S")
                header["ok"] = completed_ok
                lines[0] = json.dumps(header)
                self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass


def _update_undo_header(path: Path, **header_updates) -> bool:
    """Read the undo log, apply `header_updates` to the meta header, write back.
    Used by both full and partial consumption marking. Returns True on success."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return False
        header = json.loads(lines[0])
        header.update(header_updates)
        lines[0] = json.dumps(header)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! Could not update undo log header: {e}", file=sys.stderr)
        return False


def _mark_undo_log_consumed(path: Path) -> bool:
    """Set undone=true in the meta header. Returns True on success."""
    return _update_undo_header(
        path,
        undone=True,
        undone_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _mark_entries_consumed(path: Path, indices: list[int],
                            total_entries: int) -> bool:
    """Mark specific entry indices as already-undone. If all entries are now
    consumed, also flip the full `undone` flag so the log behaves the same
    as a fully-undone one for `cardo undo` / `cardo undo --list`."""
    parsed = _read_undo_log(path)
    if parsed is None:
        return False
    header, _ = parsed
    already_done: set[int] = set(header.get("undone_entries", []))
    already_done.update(indices)
    updates: dict[str, Any] = {
        "undone_entries": sorted(already_done),
        "partially_undone_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if len(already_done) >= total_entries:
        updates["undone"] = True
        updates["undone_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return _update_undo_header(path, **updates)


def _read_undo_log(path: Path) -> tuple[dict, list[dict]] | None:
    """Parse an undo log file. Returns (header, entries) or None on failure."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        header = json.loads(lines[0])
        if not header.get("_meta"):
            return None
        entries = [json.loads(line) for line in lines[1:] if line.strip()]
        return (header, entries)
    except json.JSONDecodeError:
        return None


def find_recent_undo_logs(limit: int = 20) -> list[tuple[Path, dict, list[dict]]]:
    """Return recent undo logs newest-first. Each tuple is (path, header, entries).
    Skips files we can't parse so the list stays useful."""
    if not UNDO_DIR.exists():
        return []
    candidates = sorted(UNDO_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    out: list[tuple[Path, dict, list[dict]]] = []
    for p in candidates[:limit * 2]:  # over-fetch in case some don't parse
        parsed = _read_undo_log(p)
        if parsed is None:
            continue
        header, entries = parsed
        out.append((p, header, entries))
        if len(out) >= limit:
            break
    return out


class RunSummary:
    """Tracks the outcome of every action in a single command run.

    At the end, .print_summary() renders a "what happened" report. If a log
    file was opened, each action is also recorded as one timestamped line.

    If an UndoLog is attached (via `attach_undo`), reversible actions are
    additionally recorded there via record_move()/record_rename()/etc.
    """

    def __init__(self, command: str, log_path: Path | None = None):
        self.command = command
        self.start = time.monotonic()
        self.start_wall = time.strftime("%Y-%m-%d %H:%M:%S")
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self._log_file = self._open_log(log_path) if log_path is not None else None
        self.undo: UndoLog | None = None

    def attach_undo(self, undo: UndoLog) -> None:
        """Attach an UndoLog so reversible actions get recorded twice:
        once in this summary's human log, once in the undo file."""
        self.undo = undo

    def record_move(self, src: Path, dst: Path) -> None:
        """Record a successful move/organize. Logs in both places."""
        self.ok(f"{src} → {dst}")
        if self.undo is not None:
            self.undo.record("move", **{"from": str(src), "to": str(dst)})

    def record_rename(self, old: Path, new: Path) -> None:
        """Record a successful rename. Logs in both places."""
        self.ok(f"{old} → {new}")
        if self.undo is not None:
            self.undo.record("rename", **{"from": str(old), "to": str(new)})

    def record_rmdir(self, path: Path) -> None:
        """Record a successful empty-directory removal."""
        self.ok(f"removed empty directory {path}")
        if self.undo is not None:
            self.undo.record("rmdir", path=str(path))

    def _open_log(self, log_path: Path):
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = log_path.open("a", encoding="utf-8")
            quoted = " ".join(shlex.quote(a) for a in sys.argv[1:])
            f.write(f"\n=== {self.start_wall}  cardo {self.command}  argv={quoted} ===\n")
            f.flush()
            return f
        except OSError as e:
            print(f"  ! Could not open log file {log_path}: {e}", file=sys.stderr)
            return None

    def ok(self, description: str) -> None:
        self.succeeded.append(description)
        self._write_log("OK", description)

    def fail(self, description: str, reason: str) -> None:
        self.failed.append((description, reason))
        self._write_log("FAIL", f"{description} — {reason}")

    def skip(self, description: str, reason: str) -> None:
        self.skipped.append((description, reason))
        self._write_log("SKIP", f"{description} — {reason}")

    def _write_log(self, level: str, msg: str) -> None:
        if self._log_file is None:
            return
        try:
            self._log_file.write(f"  [{time.strftime('%H:%M:%S')}] {level:<4}  {msg}\n")
            self._log_file.flush()
        except OSError:
            pass  # log writes shouldn't kill the run

    def print_summary(self) -> None:
        elapsed = time.monotonic() - self.start
        total = len(self.succeeded) + len(self.failed) + len(self.skipped)
        if total == 0:
            return  # commands that did no per-file work skip the summary

        print()
        print("  " + "─" * 48)
        print(f"  Summary for {self.command} (took {humantime(elapsed)}):")
        print(f"    ✓ Succeeded: {len(self.succeeded)}")
        if self.skipped:
            print(f"    – Skipped:   {len(self.skipped)}")
        if self.failed:
            print(f"    ✗ Failed:    {len(self.failed)}")
            # Group failures so a flood of identical errors collapses.
            reasons = Counter(reason for _, reason in self.failed)
            print(f"    Failure breakdown:")
            for reason, count in reasons.most_common():
                label = reason if len(reason) <= 70 else reason[:67] + "…"
                print(f"      ({count}×) {label}")
        if self._log_file is not None:
            print(f"  Log file: {self._log_file.name}")
        if self.undo is not None and self.undo.entries and self.undo.path.exists():
            print(f"  Undo log: {self.undo.path}")
            print(f"  Reverse with: cardo undo")
        print("  " + "─" * 48)

    def close(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.write(
                    f"=== End ({humantime(time.monotonic() - self.start)}) ===\n"
                )
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        if self.undo is not None:
            self.undo.close(completed_ok=not self.failed)
            self.undo = None


def resolve_log_path(args) -> Path | None:
    """--log         → timestamped name in default location
       --log PATH    → that exact file
       (no --log)    → no logging"""
    log_arg = getattr(args, "log", None)
    if log_arg is None:
        return None
    if log_arg == "":  # nargs='?' with no value
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        return DEFAULT_LOG_DIR / f"cardo_{args.command}_{ts}.log"
    return Path(log_arg).expanduser()


def maybe_attach_undo(summary: RunSummary, args) -> None:
    """Attach an UndoLog to `summary` if the command is reversible and the
    user hasn't suppressed undo logging.

    Skipped on:
      - dry-run (nothing actually changes)
      - non-reversible commands (cmd not in UNDOABLE_COMMANDS)
      - --no-undo (escape hatch for users who really don't want it)
    """
    if getattr(args, "no_undo", False):
        return
    if getattr(args, "dry_run", False):
        return
    cmd = getattr(args, "command", summary.command)
    if cmd not in UNDOABLE_COMMANDS:
        return
    undo = UndoLog(cmd, sys.argv[1:])
    summary.attach_undo(undo)


# ──────────────────────────────────────────────────────────────────────────
# Preflight estimation + progress bar
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OpProfile:
    """Per-operation throughput model. Bytes-per-sec for the byte component
    plus a fixed per-file overhead in seconds for the metadata component."""
    label: str
    bytes_per_sec: int
    per_file_overhead: float

_GB = 1024 * 1024 * 1024
_HIGH = 10 * _GB   # for metadata-only ops, byte rate is effectively infinite

# Single source of truth: replaces THROUGHPUT + PER_FILE_OVERHEAD + label dict.
OP_PROFILES: dict[str, OpProfile] = {
    "copy":       OpProfile("copy",          80 * 1024 * 1024, 0.005),
    "move":       OpProfile("move",         200 * 1024 * 1024, 0.001),
    "hash":       OpProfile("dedupe (hash)",250 * 1024 * 1024, 0.002),
    "scan":       OpProfile("scan",         500 * 1024 * 1024, 0.0005),
    "rename":     OpProfile("rename",       _HIGH,             0.001),
    "search":     OpProfile("search",       _HIGH,             0.0003),
    "tree":       OpProfile("tree",         _HIGH,             0.0003),
    "clean":      OpProfile("clean",        _HIGH,             0.0005),
    "stats":      OpProfile("stats",        _HIGH,             0.0003),
    "name-clash": OpProfile("name-clash",   _HIGH,             0.0003),
    "organize":   OpProfile("organize",     200 * 1024 * 1024, 0.001),
}

# Skip the confirmation prompt below this estimated duration.
CONFIRM_THRESHOLD_SECONDS = 10


def humantime(seconds: float) -> str:
    """3725 -> '1h 2m'. Used for ETA + estimates."""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h {rem // 60}m"


def estimate(op: str, file_count: int, total_bytes: int) -> float:
    """Estimated duration in seconds for an operation."""
    if file_count == 0:
        return 0.0
    prof = OP_PROFILES.get(op, OP_PROFILES["copy"])
    return (total_bytes / prof.bytes_per_sec) + prof.per_file_overhead * file_count


def scan_for_estimate(files: Iterable[Path]) -> tuple[int, int]:
    """Count files and sum their sizes. Skips unreadable entries."""
    count = 0
    total = 0
    for f in files:
        st = safe_stat(f)
        if st is None:
            continue
        total += st.st_size
        count += 1
    return count, total


def prescan_directory(root: Path, recursive: bool = True,
                       pattern: str | None = None) -> tuple[int, int]:
    """Quickly walk a tree and return (file_count, total_bytes) for estimates."""
    return scan_for_estimate(iter_files(root, recursive=recursive, pattern=pattern))


def preflight(op: str, file_count: int, total_bytes: int, *,
              assume_yes: bool = False, dry_run: bool = False) -> bool:
    """Print an estimate. For long jobs, ask the user to confirm. Returns True
    to proceed; False after printing 'Aborted.' if the user declines."""
    eta = estimate(op, file_count, total_bytes)
    label = OP_PROFILES.get(op, OP_PROFILES["copy"]).label
    print(f"  Plan: {file_count:,} file(s), {human_size(total_bytes)} total")
    print(f"  Estimated time for {label}: ~{humantime(eta)}")
    if dry_run or assume_yes or eta < CONFIRM_THRESHOLD_SECONDS:
        return True
    try:
        ans = input("  Proceed? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans in ("y", "yes"):
        return True
    print("Aborted.")
    return False


class ProgressBar:
    """Minimal in-terminal progress bar tracking files and bytes.

    Updates at most ~10 times/second to avoid spamming the terminal.

    Thread-safe: `update()` and `finish()` take `_lock`. Concurrent
    workers can call update() without interleaving terminal writes.
    """

    def __init__(self, total_files: int, total_bytes: int, label: str = "Working"):
        self.total_files = total_files
        self.total_bytes = max(total_bytes, 1)  # avoid div-by-zero
        self.label = label
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self.last_draw = 0.0
        self._enabled = sys.stderr.isatty()
        self._lock = threading.Lock()

    def update(self, file_bytes: int = 0) -> None:
        with self._lock:
            self.done_files += 1
            self.done_bytes += file_bytes
            now = time.monotonic()
            if now - self.last_draw < 0.1 and self.done_files < self.total_files:
                return
            self.last_draw = now
            self._draw()

    def _draw(self) -> None:
        # Caller must hold _lock.
        if not self._enabled:
            return
        frac = min(max(self.done_bytes / self.total_bytes, 0.0), 1.0)
        bar_width = 30
        filled = int(bar_width * frac)
        bar = "█" * filled + "░" * (bar_width - filled)

        elapsed = time.monotonic() - self.start
        rate = self.done_bytes / elapsed if elapsed > 0 else 0
        if rate > 0 and frac < 1.0:
            eta_str = humantime((self.total_bytes - self.done_bytes) / rate)
        else:
            eta_str = "—"
        rate_str = f"{human_size(int(rate))}/s" if rate > 0 else "—/s"
        line = (f"\r  {self.label} [{bar}] "
                f"{self.done_files}/{self.total_files} files  "
                f"{human_size(self.done_bytes)}/{human_size(self.total_bytes)}  "
                f"{rate_str}  ETA {eta_str}")
        sys.stderr.write(line.ljust(110))
        sys.stderr.flush()

    def finish(self) -> None:
        with self._lock:
            if self._enabled:
                self._draw()
                sys.stderr.write("\n")
                sys.stderr.flush()
            elapsed = time.monotonic() - self.start
        print(f"  Completed in {humantime(elapsed)}.")


# ──────────────────────────────────────────────────────────────────────────
# HTML reports
# ──────────────────────────────────────────────────────────────────────────

REPORT_DIR = Path.home() / ".cardo" / "reports"

_REPORT_CSS = """
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px;
         margin: 2em auto; padding: 0 1em; color: #222; line-height: 1.5; }
  h1 { border-bottom: 2px solid #444; padding-bottom: .3em; }
  h2 { margin-top: 2em; color: #444; }
  .meta { color: #666; font-size: 0.95em; margin-bottom: 2em; }
  table.summary { border-collapse: collapse; margin-bottom: 2em; }
  table.summary th { text-align: left; padding: .3em .8em .3em 0; color: #555; font-weight: 500; }
  table.summary td { padding: .3em 0; font-variant-numeric: tabular-nums; }
  .set { margin: 1.5em 0; padding: 1em; border-radius: 6px;
         background: #f7f7f9; border-left: 4px solid #888; }
  .set.kind-duplicate { border-left-color: #c0392b; }
  .set.kind-advisory  { border-left-color: #d68910; }
  .set.kind-suspect   { border-left-color: #2874a6; }
  .set.kind-match     { border-left-color: #16a085; }
  .set h3 { margin: 0 0 .3em; font-size: 1em; }
  .set p  { margin: 0 0 .8em; color: #666; font-size: 0.9em; }
  .set table { width: 100%; border-collapse: collapse; }
  .set td { padding: .15em .5em; font-family: ui-monospace, Menlo, monospace;
            font-size: 0.85em; word-break: break-all; }
  .role { width: 5em; font-weight: 600; }
  .role.keep { color: #1e8449; }
  .role.delete { color: #c0392b; }
  .badge { font-size: 0.75em; padding: .15em .5em; background: #555;
           color: white; border-radius: 3px; vertical-align: middle; }
  code { background: #eee; padding: .1em .3em; border-radius: 3px; }
  pre.tree { background: #f7f7f9; padding: 1em; border-radius: 6px;
             font-family: ui-monospace, Menlo, monospace; font-size: 0.85em;
             line-height: 1.4; overflow-x: auto; white-space: pre; }
"""

_KIND_LABELS = {
    "duplicate": "Duplicate",
    "advisory":  "Advisory (installer/package)",
    "suspect":   "Suspect (metadata match)",
    "match":     "Match",
}


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _format_summary_value(key: str, val) -> str:
    if "bytes" in key.lower() and isinstance(val, (int, float)):
        return human_size(int(val))
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def _render_set(i: int, s: dict) -> str:
    kind_label = _KIND_LABELS.get(s.get("kind", ""), s.get("kind", ""))
    rows = []
    for j, p in enumerate(s["paths"]):
        if s["kind"] == "duplicate":
            role, role_cls = ("KEEP", "keep") if j == 0 else ("DELETE", "delete")
        else:
            role, role_cls = ("&nbsp;", "")
        rows.append(f'<tr><td class="role {role_cls}">{role}</td>'
                    f'<td class="path">{_html_escape(p)}</td></tr>')
    wasted = s["size"] * (s["count"] - 1)
    return (
        f'<div class="set kind-{s["kind"]}">'
        f'<h3>Set {i}: <code>{_html_escape(s["name"])}</code> '
        f'<span class="badge">{kind_label}</span></h3>'
        f'<p>{s["count"]} copies, {human_size(s["size"])} each, '
        f'<strong>{human_size(wasted)} reclaimable</strong></p>'
        f'<table>{"".join(rows)}</table>'
        f'</div>'
    )


def _write_html_report(command: str, root: Path, summary: dict,
                        duplicate_sets: list[dict],
                        pre_text: str | None = None) -> Path | None:
    """Write a self-contained HTML report. Returns the file path, or None on error."""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  ! Could not create report directory: {e}", file=sys.stderr)
        return None
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    safe_cmd = re.sub(r"[^a-zA-Z0-9_-]+", "_", command).strip("_")
    report_path = REPORT_DIR / f"cardo_{safe_cmd}_{ts}.html"

    set_blocks = "".join(_render_set(i, s) for i, s in enumerate(duplicate_sets, start=1))
    summary_rows = "".join(
        f"<tr><th>{_html_escape(str(k).replace('_', ' ').title())}</th>"
        f"<td>{_html_escape(_format_summary_value(k, v))}</td></tr>"
        for k, v in summary.items()
    )

    if pre_text:
        body_extra = f'<h2>Output</h2><pre class="tree">{_html_escape(pre_text)}</pre>'
    elif duplicate_sets:
        n = len(duplicate_sets)
        body_extra = f'<h2>Details ({n} set{"s" if n != 1 else ""})</h2>{set_blocks}'
    else:
        body_extra = ""

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>cardo report — {_html_escape(command)}</title>
<style>{_REPORT_CSS}</style>
</head><body>
<h1>cardo report</h1>
<p class="meta">
  <strong>Command:</strong> {_html_escape(command)}<br>
  <strong>Folder:</strong> <code>{_html_escape(str(root))}</code><br>
  <strong>Generated:</strong> {time.strftime("%Y-%m-%d %H:%M:%S")}
</p>
<h2>Summary</h2>
<table class="summary">{summary_rows}</table>
{body_extra}
</body></html>"""
    try:
        report_path.write_text(html, encoding="utf-8")
        return report_path
    except OSError as e:
        print(f"  ! Could not write report: {e}", file=sys.stderr)
        return None


def maybe_write_report(args, command: str, root: Path, summary: dict,
                        duplicate_sets: list[dict] | None = None,
                        pre_text: str | None = None,
                        indent_link: bool = True) -> None:
    """Common report-emission tail. No-op if --report wasn't passed."""
    if not getattr(args, "report", False):
        return
    path = _write_html_report(
        command=command,
        root=root,
        summary=summary,
        duplicate_sets=duplicate_sets or [],
        pre_text=pre_text,
    )
    if path:
        prefix = "\n  " if indent_link else "  "
        print(f"{prefix}HTML report saved to: {path}")


# ──────────────────────────────────────────────────────────────────────────
# Commands — shared plumbing
# ──────────────────────────────────────────────────────────────────────────

def _require_dir(p: Path) -> bool:
    """Print error to stderr and return False if `p` isn't a directory."""
    if not p.is_dir():
        print(f"✗ Not a directory: {p}", file=sys.stderr)
        return False
    return True


def _gather_copy_move_plan(src: Path, recursive: bool, pattern: str | None,
                            overwrite: bool, dst: Path) -> list[tuple[Path, Path, int]]:
    """Build (source, target, size) tuples for copy/move."""
    plan: list[tuple[Path, Path, int]] = []
    if src.is_file():
        target = dst / src.name if overwrite else unique_path(dst / src.name)
        st = safe_stat(src)
        plan.append((src, target, st.st_size if st else 0))
        return plan

    for f in iter_files(src, recursive, pattern):
        rel = f.relative_to(src)
        target = dst / rel if overwrite else unique_path(dst / rel)
        st = safe_stat(f)
        plan.append((f, target, st.st_size if st else 0))
    return plan


def _count_skipped_subfolders(src: Path) -> int:
    """How many subfolders are we ignoring in non-recursive mode?"""
    if not src.is_dir():
        return 0
    return sum(1 for p in src.iterdir() if p.is_dir())


def _run_copy_or_move(args, *, verb: str, gerund: str, op_key: str,
                        action: Callable[[Path, Path], None]) -> int:
    """Shared body of cmd_copy and cmd_move — only the action callback differs.

    `verb` is the imperative form ('copy'/'move') used in user-facing messages.
    `gerund` is the -ing form ('Copying'/'Moving') used as the progress label.
    """
    src, dst = Path(args.source), Path(args.dest)
    if not src.exists():
        print(f"✗ Source does not exist: {src}", file=sys.stderr)
        return 1
    if not require_admin_if_needed(op_key, [src, dst]):
        return 1

    plan = _gather_copy_move_plan(src, args.recursive, args.pattern, args.overwrite, dst)
    if not plan:
        print("Nothing matched.")
        return 0

    if src.is_dir() and not args.recursive:
        skipped = _count_skipped_subfolders(src)
        if skipped:
            print(f"  Note: skipping {skipped} subfolder(s) (run with -r to include them).")

    total_files = len(plan)
    total_bytes = sum(size for _, _, size in plan)

    if not preflight(op_key, total_files, total_bytes,
                     assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    # ─── Protection preflight ──────────────────────────────────────────
    # Two checks:
    #   - Source side: are we moving FROM inside a managed package? Esp. bad
    #     for `move` since the source is mutated.
    #   - Destination side: are we placing files INTO a managed package? Bad
    #     even for `copy` because copies inside an app bundle can confuse it.
    skipped_protected: list[tuple[tuple[Path, Path, int], str]] = []
    if not getattr(args, "include_unsafe", False):
        # Run install-folder detection on the source side if it's a directory.
        # For a single-file source, we still want path-segment protection.
        src_install: set[Path] = set()
        root_warnings: list[str] = []
        if src.is_dir():
            src_install, root_warnings = detect_install_folders_with_root_check(
                src, recursive=args.recursive
            )
        # Detection on the destination side only makes sense if it exists.
        dst_install: set[Path] = set()
        if dst.exists() and dst.is_dir():
            dst_install_set, dst_warnings = detect_install_folders_with_root_check(
                dst, recursive=False
            )
            dst_install = dst_install_set
            root_warnings.extend(
                w.replace(str(src), str(dst)) if "specified" in w else w
                for w in dst_warnings
            )

        all_install = src_install | dst_install

        # Check each plan entry: protect if EITHER the source or the
        # destination's parent is inside a managed package.
        def _entry_protected(entry: tuple[Path, Path, int]) -> tuple[bool, str | None]:
            f, target, _ = entry
            src_prot, src_reason = is_protected_path(f, all_install)
            if src_prot:
                return (True, f"source {src_reason}")
            tgt_prot, tgt_reason = is_protected_path(target.parent, all_install)
            if tgt_prot:
                return (True, f"destination {tgt_reason}")
            return (False, None)

        safe_plan: list[tuple[Path, Path, int]] = []
        protected: list[tuple[tuple[Path, Path, int], str]] = []
        for entry in plan:
            is_prot, reason = _entry_protected(entry)
            if is_prot:
                protected.append((entry, reason or "protected"))
            else:
                safe_plan.append(entry)

        skipped_protected = protected
        if protected or root_warnings:
            if not confirm_protection_skip(
                op_key,
                safe_count=len(safe_plan),
                protected=protected,
                path_of=lambda entry: entry[0],
                assume_yes=args.yes,
                dry_run=args.dry_run,
                root_warnings=root_warnings,
            ):
                return 0
        plan = safe_plan
        total_files = len(plan)
        total_bytes = sum(size for _, _, size in plan)

    # Only create the destination once the user has committed.
    dst.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        for f, target, _ in plan:
            print(f"  Would {verb}: {f} → {target}")
        print(f"\nWould {verb} {total_files} file(s), {human_size(total_bytes)} total.")
        return 0

    summary = RunSummary(op_key, resolve_log_path(args))
    maybe_attach_undo(summary, args)
    try:
        for (p, _, _), reason in skipped_protected:
            summary.skip(str(p), f"protected: {reason}")
        bar = ProgressBar(total_files, total_bytes, label=gerund)
        for f, target, size in plan:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                action(f, target)
                # Only moves are reversible; copy leaves the source intact so
                # there's no inverse to record.
                if op_key == "move":
                    summary.record_move(f, target)
                else:
                    summary.ok(f"{f} → {target}")
            except OSError as e:
                summary.fail(f"{f} → {target}", str(e))
                sys.stderr.write(f"\n  ! Failed to {verb} {f}: {e}\n")
            bar.update(size)
        bar.finish()
        summary.print_summary()
        return 0 if not summary.failed else 1
    finally:
        summary.close()


# ──────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────

def cmd_copy(args) -> int:
    return _run_copy_or_move(
        args, verb="copy", gerund="Copying", op_key="copy",
        action=lambda src, dst: shutil.copy2(src, dst),
    )


def cmd_move(args) -> int:
    return _run_copy_or_move(
        args, verb="move", gerund="Moving", op_key="move",
        action=lambda src, dst: shutil.move(str(src), str(dst)),
    )


def cmd_rename(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1
    if not require_admin_if_needed("rename", [root]):
        return 1

    files = sorted(iter_files(root, args.recursive, args.pattern))
    if not files:
        print("Nothing matched.")
        return 0

    plan: list[tuple[Path, Path]] = []
    for i, f in enumerate(files, start=args.start):
        new_stem = f.stem
        suffix = f.suffix
        if args.regex:
            pat, repl = args.regex
            new_stem = re.sub(pat, repl, new_stem)
        if args.prefix:
            new_stem = args.prefix + new_stem
        if args.suffix:
            new_stem = new_stem + args.suffix
        if args.lower:
            new_stem = new_stem.lower()
        if args.upper:
            new_stem = new_stem.upper()
        if args.numbered:
            new_stem = args.numbered.format(i)
        if args.ext is not None:
            suffix = "." + args.ext.lstrip(".") if args.ext else ""

        new_name = new_stem + suffix
        if new_name != f.name:
            plan.append((f, f.with_name(new_name)))

    if not plan:
        print("No names would change.")
        return 0

    total_bytes = sum((st.st_size for f, _ in plan if (st := safe_stat(f))))
    if not preflight("rename", len(plan), total_bytes,
                     assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    # Protection preflight: rename inside an install folder typically breaks
    # the app (it loads `License.txt` by name, not by content). Check the
    # source path of each entry. Skipped if --include-unsafe.
    skipped_protected: list[tuple[tuple[Path, Path], str]] = []
    if not getattr(args, "include_unsafe", False):
        install_folders, root_warnings = detect_install_folders_with_root_check(
            root, recursive=args.recursive
        )
        safe_plan, protected = partition_safe_protected(
            plan, install_folders, path_of=lambda entry: entry[0]
        )
        skipped_protected = protected
        if protected or root_warnings:
            if not confirm_protection_skip(
                "rename",
                safe_count=len(safe_plan),
                protected=protected,
                path_of=lambda entry: entry[0],
                assume_yes=args.yes,
                dry_run=args.dry_run,
                root_warnings=root_warnings,
            ):
                return 0
        plan = safe_plan

    summary = RunSummary("rename", resolve_log_path(args))
    maybe_attach_undo(summary, args)
    try:
        for (p, _), reason in skipped_protected:
            summary.skip(str(p), f"protected: {reason}")
        for old, new in plan:
            target = new if args.overwrite else unique_path(new)
            verb = "Would rename" if args.dry_run else "Renaming"
            print(f"  {verb}: {old.name} → {target.name}")
            if args.dry_run:
                continue
            if args.interactive and not confirm(f"Rename {old.name}?"):
                summary.skip(f"{old} → {target}", "user declined in interactive mode")
                continue
            try:
                old.rename(target)
                summary.record_rename(old, target)
            except OSError as e:
                summary.fail(f"{old} → {target}", str(e))
                sys.stderr.write(f"  ! Failed: {e}\n")
        if args.dry_run:
            print(f"\nWould rename {len(plan)} file(s).")
        else:
            summary.print_summary()
        return 0 if not summary.failed else 1
    finally:
        summary.close()


# ──────────────────────────────────────────────────────────────────────────
# Dedupe — broken into phases for readability
# ──────────────────────────────────────────────────────────────────────────

def _dedupe_quick(args, root: Path) -> int:
    """Metadata-only duplicate scan: groups by (size, name) without reading any
    file content. Fast triage tool — produces a report, never deletes."""
    print(f"  Quick scan of {root} (metadata only, no hashing)…")
    install_folders: set[Path] = set()
    if not args.include_unsafe:
        detected = find_install_folders(root, args.recursive)
        install_folders = set(detected.keys())
        if detected:
            print(f"  Skipping {len(detected)} installation folder(s).")

    min_size_bytes = max(args.min_size, 0) * 1024
    by_key: dict[tuple[int, str], list[Path]] = defaultdict(list)
    total = 0
    for f in iter_files(root, args.recursive, args.pattern):
        total += 1
        if total % 5000 == 0:
            sys.stderr.write(f"\r  Scanned {total:,} files…")
            sys.stderr.flush()
        if not args.include_unsafe:
            cls, _ = classify_file(f, install_folders)
            if cls == "blocked":
                continue
        st = safe_stat(f)
        if st is None:
            continue
        if st.st_size < min_size_bytes and not args.include_empty:
            continue
        by_key[(st.st_size, f.name)].append(f)

    if total >= 5000:
        sys.stderr.write(f"\r  Scanned {total:,} files.        \n")
        sys.stderr.flush()
    else:
        print(f"  Scanned {total:,} files.")

    suspects = [(k, v) for k, v in by_key.items() if len(v) > 1]
    if not suspects:
        print("\n  No suspect duplicates found by metadata.")
        maybe_write_report(
            args, command="dedupe (quick mode)", root=root,
            summary={
                "total_files_scanned": total,
                "suspect_sets": 0,
                "suspect_files": 0,
                "potential_savings_bytes": 0,
            },
        )
        return 0

    suspects.sort(key=lambda kv: (-kv[0][0] * (len(kv[1]) - 1), kv[0][1]))
    total_suspect_files = sum(len(v) - 1 for _, v in suspects)
    potential_savings = sum(k[0] * (len(v) - 1) for k, v in suspects)

    print(f"\n  ─── Quick scan results ───")
    print(f"  Found {len(suspects):,} suspect set(s) by name + size match.")
    print(f"  Potential space if all suspects were real duplicates: "
          f"{human_size(potential_savings)} across {total_suspect_files:,} files.\n")

    print("  Top 20 suspect sets (sorted by potential savings):")
    for i, ((size, name), files) in enumerate(suspects[:20], start=1):
        wasted = size * (len(files) - 1)
        print(f"\n  [{i}] '{name}'  ({human_size(size)} each, {len(files)} copies, "
              f"{human_size(wasted)} reclaimable):")
        for f in files:
            print(f"        {f}")
    if len(suspects) > 20:
        print(f"\n  …and {len(suspects) - 20} more suspect set(s) (run 'standard' "
              f"or 'paranoid' mode to verify and delete).")

    maybe_write_report(
        args, command="dedupe (quick mode)", root=root,
        summary={
            "total_files_scanned": total,
            "suspect_sets": len(suspects),
            "suspect_files": total_suspect_files,
            "potential_savings_bytes": potential_savings,
        },
        duplicate_sets=[
            {"name": name, "size": size, "count": len(files),
             "paths": [str(p) for p in files], "kind": "suspect"}
            for (size, name), files in suspects
        ],
    )

    print("\n  Quick mode never deletes. Re-run with mode 'standard' or "
          "'paranoid' to verify and remove.")
    return 0


def _dedupe_find_install_folders(root: Path, args) -> set[Path]:
    """Phase 0: locate installation folders so we can skip their contents."""
    if args.include_unsafe:
        return set()
    print(f"  Looking for installation folders under {root}…")
    detected = find_install_folders(root, args.recursive)
    if detected:
        print(f"\n  ⚠ Detected {len(detected)} installation folder(s) — "
              f"their contents will NOT be touched:")
        for folder, evidence in sorted(detected.items()):
            try:
                rel = folder.relative_to(root)
            except ValueError:
                rel = folder
            print(f"    • {rel}/")
            for ev in evidence:
                print(f"        — {ev}")
        print("  (Pass --include-unsafe to dedupe inside these — not recommended.)\n")
    return set(detected.keys())


def _dedupe_scan_phase(root: Path, args, install_folders: set[Path]):
    """Phase 1: walk + classify + group by size. Returns ScanResult-shaped dict."""
    min_size_bytes = max(args.min_size, 0) * 1024
    print(f"  Scanning {root}…")
    if min_size_bytes > 0:
        print(f"  (Skipping files smaller than {human_size(min_size_bytes)} — "
              f"override with --min-size 0.)")

    by_size_normal: dict[int, list[Path]] = defaultdict(list)
    by_size_advisory: dict[int, list[Path]] = defaultdict(list)
    blocked_count = 0
    block_reasons: dict[str, int] = defaultdict(int)
    too_small_count = 0
    total_scanned = 0

    for f in iter_files(root, args.recursive, args.pattern):
        total_scanned += 1
        if total_scanned % 5000 == 0:
            sys.stderr.write(f"\r  Scanned {total_scanned:,} files…")
            sys.stderr.flush()
        if args.include_unsafe:
            classification, reason = "normal", None
        else:
            classification, reason = classify_file(f, install_folders)
            if classification == "blocked":
                blocked_count += 1
                block_reasons[reason or "unknown"] += 1
                continue
        st = safe_stat(f)
        if st is None:
            continue
        if (st.st_size == 0 and not args.include_empty) or st.st_size < min_size_bytes:
            too_small_count += 1
            continue
        if classification == "advisory":
            by_size_advisory[st.st_size].append(f)
        else:
            by_size_normal[st.st_size].append(f)

    if total_scanned >= 5000:
        sys.stderr.write(f"\r  Scanned {total_scanned:,} files.        \n")
        sys.stderr.flush()
    else:
        print(f"  Scanned {total_scanned:,} files.")

    if too_small_count:
        print(f"  Skipped {too_small_count:,} small/empty file(s) below the size threshold.")
    if blocked_count:
        print(f"\n  Skipped {blocked_count:,} file(s) inside managed packages or system files:")
        for reason, count in sorted(block_reasons.items(), key=lambda x: -x[1])[:8]:
            print(f"    {count:>6}× {reason}")
        if len(block_reasons) > 8:
            print(f"    … and {len(block_reasons) - 8} other category/categories")
        print("  (Pass --include-unsafe to include these — not recommended.)")

    return {
        "by_size_normal": by_size_normal,
        "by_size_advisory": by_size_advisory,
        "total_scanned": total_scanned,
    }


def _resolve_workers(requested: int) -> int:
    """Translate the --workers / config value into an actual thread count.

    0 means 'auto': we cap at 8 because hashing throughput saturates quickly
    on most disks, and extra threads then just add contention. Negative or
    bogus values fall back to 1.
    """
    if requested is None or requested < 0:
        return 1
    if requested == 0:
        return min(8, os.cpu_count() or 1)
    return requested


def _hash_one_prefix(f: Path) -> tuple[Path, int, str | None, str | None]:
    """Worker: prefix-hash one file. Returns (path, byte_count, prefix_hash, error).

    Tuple shape lets the main thread update the progress bar consistently.
    `prefix_hash` is None on error or unreadable; the special string "empty"
    represents a zero-byte file (matching the serial code path).
    """
    st = safe_stat(f)
    if st is None:
        return (f, 0, None, "could not stat")
    if st.st_size == 0:
        return (f, 0, "empty", None)
    try:
        ph = file_hash_prefix(f)
    except OSError as e:
        return (f, 0, None, str(e))
    return (f, min(st.st_size, 65536), ph or None, None)


def _hash_one_full(f: Path, cache: HashCache | None
                   ) -> tuple[Path, int, str | None, str | None]:
    """Worker: full hash one file (via cache when available)."""
    st = safe_stat(f)
    size = st.st_size if st else 0
    try:
        h = cached_file_hash(f, cache)
    except OSError as e:
        return (f, 0, None, str(e))
    if not h:
        return (f, size, None, "hash returned empty")
    return (f, size, h, None)


def _dedupe_hash_candidates(candidates: list[Path], cache: HashCache | None,
                             workers: int = 1
                             ) -> dict[str, list[Path]]:
    """Stage A (prefix hash) + Stage B (full hash). Returns hash → paths.

    `workers` is the resolved thread count (1 = serial). Errors from
    individual files are collected and printed after each stage so they
    don't shred the progress bar.
    """
    # ─── Stage A: prefix hash ─────────────────────────────────────────────
    print(f"  Stage 1/2: quick prefix hash (64 KB per file) to filter candidates"
          f"{f' (workers={workers})' if workers > 1 else ''}…")
    prefix_bytes_total = sum(
        min(st.st_size, 65536) for f in candidates if (st := safe_stat(f))
    )
    bar = ProgressBar(len(candidates), prefix_bytes_total, label="Prefix-hash")
    by_prefix: dict[tuple[int, str], list[Path]] = defaultdict(list)
    errors: list[str] = []

    def _process_prefix_result(result: tuple[Path, int, str | None, str | None]) -> None:
        f, processed, ph, err = result
        if err:
            errors.append(f"  ! Skipped {f}: {err}")
        elif ph:
            st = safe_stat(f)
            size = st.st_size if st else 0
            by_prefix[(size, ph)].append(f)
        bar.update(processed)

    if workers <= 1:
        for f in candidates:
            _process_prefix_result(_hash_one_prefix(f))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(_hash_one_prefix, candidates):
                _process_prefix_result(result)
    bar.finish()
    for line in errors:
        sys.stderr.write(line + "\n")

    survivors = [g for g in by_prefix.values() if len(g) > 1]
    files_for_full = [f for g in survivors for f in g]
    if not files_for_full:
        return {}

    bytes_for_full = sum(st.st_size for f in files_for_full if (st := safe_stat(f)))
    eliminated = len(candidates) - len(files_for_full)
    pct = (eliminated / len(candidates)) * 100 if candidates else 0
    print(f"  Prefix hash eliminated {eliminated:,} files ({pct:.0f}%). "
          f"Full-hashing remaining {len(files_for_full):,} files ({human_size(bytes_for_full)})…")

    # ─── Stage B: full SHA-256 ───────────────────────────────────────────
    bar = ProgressBar(len(files_for_full), bytes_for_full, label="Full hash")
    hashes: dict[str, list[Path]] = defaultdict(list)
    errors.clear()

    def _process_full_result(result: tuple[Path, int, str | None, str | None]) -> None:
        f, processed, h, err = result
        if err:
            errors.append(f"  ! Skipped {f}: {err}")
        elif h:
            hashes[h].append(f)
        bar.update(processed)

    if workers <= 1:
        for f in files_for_full:
            _process_full_result(_hash_one_full(f, cache))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_hash_one_full, f, cache): f for f in files_for_full}
            for fut in as_completed(futures):
                _process_full_result(fut.result())
    bar.finish()
    for line in errors:
        sys.stderr.write(line + "\n")
    return hashes


def _dedupe_partition_groups(hashes: dict[str, list[Path]],
                              install_folders: set[Path],
                              include_unsafe: bool
                              ) -> tuple[list[list[Path]], list[list[Path]]]:
    """Split confirmed duplicate groups into normal and advisory."""
    normal_groups: list[list[Path]] = []
    advisory_groups: list[list[Path]] = []
    for group in hashes.values():
        if len(group) <= 1:
            continue
        if include_unsafe:
            normal_groups.append(group)
            continue
        if any(classify_file(p, install_folders)[0] == "advisory" for p in group):
            advisory_groups.append(group)
        else:
            normal_groups.append(group)
    return normal_groups, advisory_groups


def _dedupe_report_advisory(advisory_groups: list[list[Path]]) -> None:
    """Print advisory installer/package duplicates — never auto-deleted."""
    if not advisory_groups:
        return
    adv_wasted = sum(
        (st.st_size if (st := safe_stat(g[0])) else 0) * (len(g) - 1)
        for g in advisory_groups
    )
    print(f"\n  ─── Advisory: duplicate installer/package files ───")
    print(f"  Found {len(advisory_groups)} set(s) — {human_size(adv_wasted)} "
          f"of redundant installers. NOT auto-deleted; review and remove manually.")
    for i, group in enumerate(advisory_groups, start=1):
        group.sort(key=lambda p: (len(str(p)), str(p)))
        st = safe_stat(group[0])
        size = st.st_size if st else 0
        print(f"\n    Set {i} ({human_size(size)} each, {len(group)} copies):")
        for p in group:
            print(f"      {p}")
    print()


def ask_deletion_mode(total_victims: int) -> str:
    """Ask the user how to handle deletions. Returns 'bulk', 'per-file', or 'cancel'."""
    print(f"\n  How would you like to proceed?")
    print(f"    1. Delete all {total_victims:,} duplicates now (one confirmation)")
    print(f"    2. Confirm each deletion individually")
    print(f"    3. Cancel — don't delete anything")
    while True:
        try:
            choice = input("  Choose [1/2/3]: ").strip()
        except EOFError:
            return "cancel"
        if choice == "1":
            if confirm(f"  Confirm: delete all {total_victims:,} files now?"):
                return "bulk"
            continue
        if choice == "2":
            return "per-file"
        if choice == "3":
            return "cancel"
        print("  Not a valid choice.")


def _dedupe_execute(normal_groups: list[list[Path]], total_victims: int,
                     total_wasted: int, args, summary: RunSummary
                     ) -> tuple[int, int, int]:
    """Execute the deletions. Returns (deleted_count, bytes_reclaimed, verify_failures)."""
    if args.dry_run:
        mode = "dry-run"
    elif args.interactive:
        mode = "per-file"
    else:
        mode = ask_deletion_mode(total_victims)
        if mode == "cancel":
            print("Cancelled. Nothing deleted.")
            return 0, 0, 0

    paranoid = getattr(args, "mode", "standard") == "paranoid"
    if paranoid:
        print(f"\n  Paranoid mode: each deletion will be confirmed by "
              f"byte-by-byte comparison with the keeper.")
    use_trash = bool(getattr(args, "trash", False))
    if use_trash:
        print(f"  Trash mode: removals go to the OS trash (recoverable).")

    # User-facing verb for prompts and log entries — kept consistent everywhere.
    action_verb = "TRASH" if use_trash else "DELETE"
    action_past = "trashed" if use_trash else "deleted"

    deleted = 0
    bytes_reclaimed = 0
    verify_failures = 0

    for i, group in enumerate(normal_groups, start=1):
        group.sort(key=lambda p: (len(str(p)), str(p)))
        keeper, victims = group[0], group[1:]
        st = safe_stat(keeper)
        size = st.st_size if st else 0

        print(f"\n  [Set {i}/{len(normal_groups)}] "
              f"({human_size(size)} each, {human_size(size * len(victims))} reclaimable):")
        print(f"    KEEP   {keeper}")

        for v in victims:
            if mode == "dry-run":
                print(f"    {action_verb} {v}  (dry-run)")
                continue
            print(f"    {action_verb} {v}")
            if mode == "per-file":
                prompt_word = "Trash" if use_trash else "Delete"
                if not confirm(f"  {prompt_word}? ({deleted}/{total_victims} done so far)"):
                    summary.skip(str(v), "user declined in per-file mode")
                    continue
            if paranoid and not files_are_identical(keeper, v):
                verify_failures += 1
                msg = ("binary verification FAILED — files are NOT byte-identical "
                       "despite matching hashes (highly unusual; not removed)")
                print(f"      ! {msg}", file=sys.stderr)
                summary.fail(str(v), msg)
                continue
            if use_trash:
                ok, err = _send_to_trash(v)
                if ok:
                    deleted += 1
                    bytes_reclaimed += size
                    summary.ok(f"{action_past} duplicate of {keeper}: {v}")
                else:
                    summary.fail(str(v), err or "unknown trash failure")
                    print(f"      ! Trash failed: {err}", file=sys.stderr)
            else:
                try:
                    v.unlink()
                    deleted += 1
                    bytes_reclaimed += size
                    summary.ok(f"{action_past} duplicate of {keeper}: {v}")
                except OSError as e:
                    summary.fail(str(v), str(e))
                    print(f"      ! Failed: {e}", file=sys.stderr)

    if mode == "dry-run":
        verb = "would trash" if use_trash else "would reclaim"
        print(f"\n  {verb.capitalize()} {human_size(total_wasted)} across {len(normal_groups)} set(s).")
    else:
        verb = "Trashed" if use_trash else "Deleted"
        print(f"\n  {verb} {deleted:,} of {total_victims:,} file(s), "
              f"reclaimed {human_size(bytes_reclaimed)}.")
        if verify_failures:
            print(f"  ⚠ {verify_failures} file(s) failed binary verification "
                  f"and were preserved.")
        summary.print_summary()

    return deleted, bytes_reclaimed, verify_failures


def cmd_dedupe(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1
    if not require_admin_if_needed("dedupe", [root]):
        return 1

    # Fail fast: if --trash was requested but send2trash isn't installed,
    # tell the user before we burn time scanning a big tree.
    use_trash, trash_err = trash_or_warn_if_requested(args)
    if trash_err:
        print(trash_err, file=sys.stderr)
        return 1

    mode = getattr(args, "mode", "standard")
    if mode == "quick":
        return _dedupe_quick(args, root)

    install_folders = _dedupe_find_install_folders(root, args)
    scan = _dedupe_scan_phase(root, args, install_folders)
    by_size_normal = scan["by_size_normal"]
    by_size_advisory = scan["by_size_advisory"]

    normal_candidates = [g for g in by_size_normal.values() if len(g) > 1]
    advisory_candidates = [g for g in by_size_advisory.values() if len(g) > 1]

    if not normal_candidates and not advisory_candidates:
        print("\n  No size-matched candidates — nothing to dedupe.")
        return 0

    files_to_hash = (
        [f for g in normal_candidates for f in g]
        + [f for g in advisory_candidates for f in g]
    )
    bytes_to_hash = sum(st.st_size for f in files_to_hash if (st := safe_stat(f)))
    print(f"\n  Found {len(files_to_hash):,} files with matching sizes "
          f"({human_size(bytes_to_hash)}).")

    if not preflight("hash", len(files_to_hash), bytes_to_hash,
                     assume_yes=args.yes, dry_run=False):
        return 0

    use_cache = not getattr(args, "no_cache", False)
    cache = HashCache() if use_cache else None
    if cache is not None and cache.data:
        print(f"  Hash cache loaded with {len(cache.data):,} previously seen file(s).")

    workers = _resolve_workers(getattr(args, "workers", 0))
    hashes = _dedupe_hash_candidates(files_to_hash, cache, workers=workers)
    if not hashes:
        print("  No duplicates found (all files differ in their first 64 KB).")
        return 0
    if cache is not None:
        print(f"  Hash cache: {cache.stats()}.")
        cache.save()

    normal_groups, advisory_groups = _dedupe_partition_groups(
        hashes, install_folders, args.include_unsafe
    )
    _dedupe_report_advisory(advisory_groups)

    if not normal_groups:
        print("  No deletable duplicates found.")
        # Even with no normal groups, advisory ones may warrant a report.
        maybe_write_report(
            args, command=f"dedupe ({mode} mode)", root=root,
            summary={
                "duplicate_sets_found": 0,
                "advisory_sets_found": len(advisory_groups),
                "files_deleted": 0,
                "bytes_reclaimed": 0,
                "verification_failures": 0,
            },
            duplicate_sets=[
                {"name": g[0].name,
                 "size": (st.st_size if (st := safe_stat(g[0])) else 0),
                 "count": len(g),
                 "paths": [str(p) for p in g],
                 "kind": "advisory"}
                for g in advisory_groups
            ],
        )
        return 0

    total_victims = sum(len(g) - 1 for g in normal_groups)
    total_wasted = sum(
        (st.st_size if (st := safe_stat(g[0])) else 0) * (len(g) - 1)
        for g in normal_groups
    )
    print(f"  ─── Found {len(normal_groups)} duplicate set(s) ───")
    print(f"  {total_victims:,} file(s) can be deleted, reclaiming {human_size(total_wasted)}.")

    summary = RunSummary("dedupe", resolve_log_path(args))
    try:
        deleted, bytes_reclaimed, verify_failures = _dedupe_execute(
            normal_groups, total_victims, total_wasted, args, summary
        )

        maybe_write_report(
            args, command=f"dedupe ({mode} mode)", root=root,
            summary={
                "duplicate_sets_found": len(normal_groups),
                "advisory_sets_found": len(advisory_groups),
                "files_deleted": deleted,
                "bytes_reclaimed": bytes_reclaimed,
                "verification_failures": verify_failures,
            },
            duplicate_sets=(
                [{"name": g[0].name,
                  "size": (st.st_size if (st := safe_stat(g[0])) else 0),
                  "count": len(g),
                  "paths": [str(p) for p in g],
                  "kind": "duplicate"}
                 for g in normal_groups]
                + [{"name": g[0].name,
                    "size": (st.st_size if (st := safe_stat(g[0])) else 0),
                    "count": len(g),
                    "paths": [str(p) for p in g],
                    "kind": "advisory"}
                   for g in advisory_groups]
            ),
        )
        return 0 if not summary.failed else 1
    finally:
        summary.close()


def cmd_name_clash(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1

    file_count, total_bytes = prescan_directory(root, recursive=True, pattern=args.pattern)
    if file_count == 0:
        print("Nothing matched.")
        return 0
    if not preflight("name-clash", file_count, total_bytes,
                     assume_yes=args.yes, dry_run=False):
        return 0

    # Group files by the key the user picked.
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in iter_files(root, recursive=True, pattern=args.pattern):
        key = f.stem if args.ignore_ext else f.name
        if args.ignore_case:
            key = key.lower()
        groups[key].append(f)

    clashes = [(k, v) for k, v in groups.items() if len(v) > 1]
    if not clashes:
        print("No name collisions found.")
        return 0

    # Largest groups first, then alphabetical.
    clashes.sort(key=lambda kv: (-len(kv[1]), kv[0]))

    identical_groups = 0
    differ_groups = 0
    report_sets: list[dict] = []

    for name, files in clashes:
        sizes: set[int] = set()
        rows: list[tuple[Path, int | None, float | None]] = []
        for f in files:
            st = safe_stat(f)
            if st is None:
                rows.append((f, None, None))
            else:
                sizes.add(st.st_size)
                rows.append((f, st.st_size, st.st_mtime))

        likely_identical = len(sizes) == 1 and rows and all(r[1] is not None for r in rows)
        marker = "✓ same size" if likely_identical else "✗ differ"
        if likely_identical:
            identical_groups += 1
        else:
            differ_groups += 1

        print(f"\n  '{name}'  ({len(files)} copies, {marker})")
        rows.sort(key=lambda r: (r[2] or 0), reverse=True)
        for f, size, mtime in rows:
            if size is None:
                print(f"      [unreadable]  {f}")
                continue
            when = time.strftime("%Y-%m-%d", time.localtime(mtime))
            print(f"      {human_size(size):>10}  {when}  {f}")

        if getattr(args, "report", False):
            rep_size = max(sizes) if sizes else 0
            report_sets.append({
                "name": name, "size": rep_size, "count": len(files),
                "paths": [str(p) for p in files], "kind": "suspect",
            })

    print(f"\n{len(clashes)} name collision(s): "
          f"{identical_groups} with matching sizes, {differ_groups} with differing sizes.")
    print("(This is a report only — nothing was changed. "
          "Use `dedupe` if you want to remove byte-identical copies.)")

    maybe_write_report(
        args, command="name-clash", root=root,
        summary={
            "total_files_scanned": file_count,
            "collisions_found": len(clashes),
            "same_size_collisions": identical_groups,
            "differing_size_collisions": differ_groups,
        },
        duplicate_sets=report_sets,
    )
    return 0


def cmd_organize(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1
    if not require_admin_if_needed("organize", [root]):
        return 1

    files = list(iter_files(root, args.recursive, args.pattern))
    if not files:
        print("Nothing matched.")
        return 0

    # Filter to files that would actually move (skip ones already in place).
    to_move = [f for f in files if f.parent != (root / category_for(f.suffix))]
    if not to_move:
        print("All files are already organized.")
        return 0

    total_bytes = sum(st.st_size for f in to_move if (st := safe_stat(f)))
    if not preflight("organize", len(to_move), total_bytes,
                     assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    # ─── Protection preflight ──────────────────────────────────────────
    # organize shuffles files into category folders inside `root`. If `root`
    # IS an install folder or any of the files-to-move live inside one, we
    # protect them. Skipped if --include-unsafe.
    skipped_protected: list[tuple[Path, str]] = []
    if not getattr(args, "include_unsafe", False):
        install_folders, root_warnings = detect_install_folders_with_root_check(
            root, recursive=args.recursive
        )
        safe_plan, protected = partition_safe_protected(to_move, install_folders)
        skipped_protected = protected
        if protected or root_warnings:
            if not confirm_protection_skip(
                "organize",
                safe_count=len(safe_plan),
                protected=protected,
                assume_yes=args.yes,
                dry_run=args.dry_run,
                root_warnings=root_warnings,
            ):
                return 0
        to_move = safe_plan

    summary = RunSummary("organize", resolve_log_path(args))
    maybe_attach_undo(summary, args)
    try:
        for p, reason in skipped_protected:
            summary.skip(str(p), f"protected: {reason}")
        for f in to_move:
            cat = category_for(f.suffix)
            dest_dir = root / cat
            target = unique_path(dest_dir / f.name)
            verb = "Would move" if args.dry_run else "Moving"
            print(f"  {verb}: {f.name} → {cat}/")
            if args.dry_run:
                continue
            try:
                dest_dir.mkdir(exist_ok=True)
                shutil.move(str(f), str(target))
                summary.record_move(f, target)
            except OSError as e:
                summary.fail(f"{f} → {cat}/", str(e))
                sys.stderr.write(f"  ! Failed: {e}\n")
        if args.dry_run:
            print(f"\nWould organize {len(to_move)} file(s).")
        else:
            summary.print_summary()
        return 0 if not summary.failed else 1
    finally:
        summary.close()


def cmd_search(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1

    now = time.time()
    min_b = args.min_size * 1024 if args.min_size else None
    max_b = args.max_size * 1024 if args.max_size else None
    ext_filter = args.ext.lower().lstrip(".") if args.ext else None

    # Combine prescan + filter into a single pass. We grab stat() once, use it
    # for the size filter, and remember sizes for sorting — avoiding the
    # O(n log n) stat calls the previous version did inside the sort.
    matches: list[tuple[Path, int, float]] = []
    scanned_count = 0
    scanned_bytes = 0
    for f in iter_files(root, recursive=True, pattern=args.pattern):
        st = safe_stat(f)
        if st is None:
            continue
        scanned_count += 1
        scanned_bytes += st.st_size
        if ext_filter is not None and f.suffix.lower().lstrip(".") != ext_filter:
            continue
        if min_b is not None and st.st_size < min_b:
            continue
        if max_b is not None and st.st_size > max_b:
            continue
        age = now - st.st_mtime
        if args.newer_than is not None and age > args.newer_than * 86400:
            continue
        if args.older_than is not None and age < args.older_than * 86400:
            continue
        matches.append((f, st.st_size, st.st_mtime))

    # Preflight runs *after* the scan because the scan IS the work. The estimate
    # was previously based on a separate prescan walk that doubled the work.
    if scanned_count == 0:
        print("Nothing matched.")
        return 0
    if not preflight("search", scanned_count, scanned_bytes,
                     assume_yes=args.yes, dry_run=False):
        return 0

    matches.sort(key=lambda r: r[1], reverse=True)
    for f, size, mtime in matches:
        age_days = (now - mtime) / 86400
        print(f"  {human_size(size):>10}  {age_days:6.1f}d  {f}")
    print(f"\n{len(matches)} match(es).")

    if getattr(args, "report", False):
        criteria_parts = []
        if args.pattern:    criteria_parts.append(f"pattern={args.pattern}")
        if args.ext:        criteria_parts.append(f"ext={args.ext}")
        if args.min_size:   criteria_parts.append(f"min={args.min_size} KB")
        if args.max_size:   criteria_parts.append(f"max={args.max_size} KB")
        if args.newer_than: criteria_parts.append(f"newer than {args.newer_than}d")
        if args.older_than: criteria_parts.append(f"older than {args.older_than}d")
        maybe_write_report(
            args, command="search", root=root,
            summary={
                "criteria": ", ".join(criteria_parts) if criteria_parts else "(no filters)",
                "matches_found": len(matches),
                "total_bytes": sum(size for _, size, _ in matches),
            },
            duplicate_sets=[
                {"name": f.name, "size": size, "count": 1,
                 "paths": [str(f)], "kind": "match"}
                for f, size, _ in matches
            ],
        )
    return 0


def cmd_tree(args) -> int:
    root = Path(args.directory)
    if not root.exists():
        print(f"✗ Not found: {root}", file=sys.stderr)
        return 1

    if root.is_dir():
        file_count, total_bytes = prescan_directory(root, recursive=True)
        if not preflight("tree", file_count, total_bytes,
                         assume_yes=args.yes, dry_run=False):
            return 0

    captured_lines: list[str] = []
    want_report = getattr(args, "report", False)

    def emit(line: str) -> None:
        print(line)
        if want_report:
            captured_lines.append(line)

    def walk(path: Path, prefix: str, depth: int) -> None:
        if args.max_depth is not None and depth > args.max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            if entry.is_dir():
                emit(f"{prefix}{branch}{entry.name}/")
                walk(entry, prefix + ("    " if last else "│   "), depth + 1)
            else:
                st = safe_stat(entry)
                size = human_size(st.st_size) if st else "?"
                emit(f"{prefix}{branch}{entry.name}  ({size})")

    emit(f"{root}/")
    walk(root, "", 1)

    if want_report:
        maybe_write_report(
            args, command="tree", root=root,
            summary={
                "max_depth": str(args.max_depth) if args.max_depth else "(unlimited)",
                "lines_in_tree": len(captured_lines),
            },
            pre_text="\n".join(captured_lines),
        )
    return 0


def cmd_clean(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1
    if not require_admin_if_needed("clean", [root]):
        return 1

    use_trash, trash_err = trash_or_warn_if_requested(args)
    if trash_err:
        print(trash_err, file=sys.stderr)
        return 1

    # ─── Phase 1: build the plan ────────────────────────────────────────
    # Walk bottom-up so nested empties get collected in one pass. We don't
    # delete anything yet — collecting first lets us run the protection
    # preflight on the full plan.
    plan: list[Path] = []
    walk_errors: list[tuple[Path, str]] = []
    for dirpath, _, _ in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                plan.append(p)
        except OSError as e:
            walk_errors.append((p, str(e)))

    if not plan and not walk_errors:
        print("  No empty directories found.")
        return 0

    # ─── Phase 2: time-estimate preflight ────────────────────────────────
    if not preflight("clean", len(plan), 0,
                     assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    # ─── Phase 3: installation-protection preflight (option C) ───────────
    # Skipped if user passed --include-unsafe.
    skipped_protected: list[tuple[Path, str]] = []
    if not getattr(args, "include_unsafe", False):
        install_folders, root_warnings = detect_install_folders_with_root_check(
            root, recursive=True
        )
        safe_plan, protected = partition_safe_protected(plan, install_folders)
        skipped_protected = protected
        if protected or root_warnings:
            if not confirm_protection_skip(
                "clean",
                safe_count=len(safe_plan),
                protected=protected,
                assume_yes=args.yes,
                dry_run=args.dry_run,
                root_warnings=root_warnings,
            ):
                return 0
        plan = safe_plan
    elif getattr(args, "include_unsafe", False):
        print(f"  --include-unsafe: skipping protection check for managed packages.")

    # ─── Phase 4: execute ────────────────────────────────────────────────
    if use_trash:
        print(f"  Trash mode: empty directories go to the OS trash (recoverable).")

    action_past = "trashed" if use_trash else "removed"

    summary = RunSummary("clean", resolve_log_path(args))
    maybe_attach_undo(summary, args)
    try:
        # Record walk errors so they appear in the summary.
        for p, err in walk_errors:
            summary.fail(str(p), err)
        # Record protection-skips so they show up under Skipped in the summary.
        for p, reason in skipped_protected:
            summary.skip(str(p), f"protected: {reason}")

        for p in plan:
            verb = "Would remove" if args.dry_run else ("Trashing" if use_trash else "Removing")
            print(f"  {verb}: {p}")
            if args.dry_run:
                continue
            if use_trash:
                ok, err = _send_to_trash(p)
                if ok:
                    # Trashed dirs are recoverable via the OS trash, not via
                    # `cardo undo` — don't record in the undo log.
                    summary.ok(f"{action_past} empty directory {p}")
                else:
                    summary.fail(str(p), err or "unknown trash failure")
                    sys.stderr.write(f"  ! Trash failed: {err}\n")
            else:
                try:
                    p.rmdir()
                    summary.record_rmdir(p)
                except OSError as e:
                    summary.fail(str(p), str(e))
                    sys.stderr.write(f"  ! Failed: {e}\n")
        if args.dry_run:
            print()
        else:
            summary.print_summary()
        return 0 if not summary.failed else 1
    finally:
        summary.close()


def cmd_stats(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1

    # Single pass: tally categories AND running totals. The previous version
    # ran prescan_directory (a full walk) before this, doubling the IO.
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [count, bytes]
    total_count = 0
    total_bytes = 0
    for f in iter_files(root, recursive=True):
        st = safe_stat(f)
        if st is None:
            continue
        cat = category_for(f.suffix)
        bucket = by_cat[cat]
        bucket[0] += 1
        bucket[1] += st.st_size
        total_count += 1
        total_bytes += st.st_size

    # Preflight now happens after the work because the work IS the scan.
    if total_count == 0:
        print(f"\n  Directory: {root}\n  (empty)")
        return 0
    if not preflight("stats", total_count, total_bytes,
                     assume_yes=args.yes, dry_run=False):
        return 0

    print(f"\n  Directory: {root}")
    print(f"  {'Category':<14} {'Count':>8} {'Size':>12}  Share")
    print(f"  {'-' * 14} {'-' * 8} {'-' * 12}  {'-' * 6}")
    for cat in sorted(by_cat, key=lambda k: by_cat[k][1], reverse=True):
        count, size = by_cat[cat]
        share = (size / total_bytes * 100) if total_bytes else 0
        bar = "█" * int(share / 4)
        print(f"  {cat:<14} {count:>8} {human_size(size):>12}  {share:5.1f}% {bar}")
    print(f"  {'-' * 14} {'-' * 8} {'-' * 12}")
    print(f"  {'TOTAL':<14} {total_count:>8} {human_size(total_bytes):>12}")

    if getattr(args, "report", False):
        summary: dict = {"total_files": total_count, "total_bytes": total_bytes}
        for cat in sorted(by_cat, key=lambda k: by_cat[k][1], reverse=True):
            count, size = by_cat[cat]
            share = (size / total_bytes * 100) if total_bytes else 0
            summary[f"{cat} (count)"] = count
            summary[f"{cat} (bytes)"] = size
            summary[f"{cat} (share %)"] = f"{share:.1f}%"
        maybe_write_report(args, command="stats", root=root, summary=summary)
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Undo
#
# Reverses the most recent un-undone reversible run. Each operation has a
# distinct inverse:
#
#   move/organize → move dst back to src
#   rename        → rename new back to old
#   rmdir         → recreate empty directory
#
# Entries are processed in reverse order (LIFO) so dependent ops untangle
# correctly: e.g. a move into a freshly-organized subfolder, when undone,
# moves the file out first and then the subfolder removal can be replayed
# in the opposite direction. (Currently the only meaningful interaction is
# rename-then-move; reverse-order undo handles it.)
# ──────────────────────────────────────────────────────────────────────────

def _list_undo_logs(args) -> int:
    """`cardo undo --list`: show recent undoable runs."""
    logs = find_recent_undo_logs()
    if not logs:
        print(f"  No undo logs in {UNDO_DIR}.")
        return 0
    print(f"  Recent runs (newest first):")
    for i, (path, header, entries) in enumerate(logs, start=1):
        partial = header.get("undone_entries", [])
        if header.get("undone"):
            status = "✓ undone"
        elif partial:
            status = f"~ partial ({len(partial)}/{len(entries)} entries done)"
        else:
            status = "  available"
        completed = header.get("completed") or "(incomplete)"
        argv = header.get("argv", [])
        argv_str = " ".join(shlex.quote(a) for a in argv)
        print(f"  {i:>2}. {status}  {len(entries):>4} action(s)  "
              f"{completed}  {header.get('command', '?')}")
        print(f"        argv: {argv_str}")
        print(f"        file: {path.name}")
    print()
    print(f"  Use `cardo undo` to reverse the most recent available run.")
    print(f"  Use `cardo restore <file>` to selectively reverse entries from "
          f"any run.")
    return 0


def _undo_one_entry(entry: dict, args) -> tuple[bool, str]:
    """Reverse a single entry. Returns (success, message)."""
    op = entry.get("op")

    if op == "move" or op == "rename":
        # Both have the same shape: from → to, and we move to back to from.
        src = Path(entry["to"])      # current location
        dst = Path(entry["from"])    # original location
        if not src.exists():
            return (False, f"current location no longer exists: {src}")
        if dst.exists() and not args.force:
            return (False, f"destination already exists (use --force to overwrite): {dst}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return (True, f"{src} → {dst}")
        except OSError as e:
            return (False, str(e))

    if op == "rmdir":
        path = Path(entry["path"])
        if path.exists():
            return (False, f"already exists: {path}")
        try:
            path.mkdir(parents=True, exist_ok=False)
            return (True, f"recreated empty directory {path}")
        except OSError as e:
            return (False, str(e))

    return (False, f"unknown operation type: {op!r}")


def cmd_undo(args) -> int:
    if getattr(args, "list", False):
        return _list_undo_logs(args)

    logs = find_recent_undo_logs()
    if not logs:
        print(f"  No undo logs in {UNDO_DIR}.")
        print(f"  Run a reversible command first (move, rename, organize, clean).")
        return 0

    # Find the most recent un-undone run.
    target: tuple[Path, dict, list[dict]] | None = None
    for path, header, entries in logs:
        if not header.get("undone"):
            target = (path, header, entries)
            break
    if target is None:
        print(f"  No un-undone runs available — all recent logs are already consumed.")
        print(f"  Use `cardo undo --list` to see the history.")
        return 0

    path, header, entries = target
    if not entries:
        print(f"  The most recent log has no recorded actions — nothing to undo.")
        _mark_undo_log_consumed(path)
        return 0

    # If this log was partially consumed by an earlier `restore`, skip the
    # entries that were already undone.
    already_done: set[int] = set(header.get("undone_entries", []))
    pending = [(i, e) for i, e in enumerate(entries) if i not in already_done]
    if not pending:
        # Defensive: if undone_entries covers everything but undone flag wasn't
        # set, flip it now.
        _mark_undo_log_consumed(path)
        print(f"  All entries in the most recent log have already been undone.")
        return 0

    cmd_label = header.get("command", "?")
    argv = header.get("argv", [])
    completed = header.get("completed") or "(incomplete)"
    argv_str = " ".join(shlex.quote(a) for a in argv)

    print(f"  Will undo this run:")
    print(f"    command:    cardo {cmd_label}")
    print(f"    argv:       {argv_str}")
    print(f"    completed:  {completed}")
    if already_done:
        print(f"    actions:    {len(pending):,} pending "
              f"({len(already_done):,} already reversed via `restore`)")
    else:
        print(f"    actions:    {len(entries):,}")
    print()

    if args.dry_run:
        # Show what would happen, in reverse order, then stop.
        print(f"  Dry run — showing planned reversal:")
        for _, entry in reversed(pending):
            op = entry.get("op")
            if op in ("move", "rename"):
                print(f"    {op:6}  {entry['to']} → {entry['from']}")
            elif op == "rmdir":
                print(f"    mkdir   {entry['path']}")
            else:
                print(f"    ?       (unknown op: {op!r})")
        print(f"\n  {len(pending)} action(s) would be reversed.")
        return 0

    if not args.yes:
        if not confirm(f"  Reverse these {len(pending)} action(s)?"):
            print("  Cancelled.")
            return 0

    summary = RunSummary(f"undo-{cmd_label}", resolve_log_path(args))
    # `undo` itself is not undoable — we don't attach an UndoLog. This is
    # deliberate; chained undo gets confusing fast.
    try:
        succeeded_indices: list[int] = []
        for idx, entry in reversed(pending):
            ok, msg = _undo_one_entry(entry, args)
            if ok:
                summary.ok(msg)
                succeeded_indices.append(idx)
                print(f"  ✓ {msg}")
            else:
                summary.fail(
                    f"{entry.get('op')}: "
                    f"{entry.get('to') or entry.get('path') or entry.get('from')}",
                    msg
                )
                sys.stderr.write(f"  ✗ {msg}\n")
        summary.print_summary()
        # Mark just the indices we actually reversed. If they cover the whole
        # log (combined with previously-undone entries), the helper flips the
        # full `undone` flag for us.
        if succeeded_indices:
            _mark_entries_consumed(path, succeeded_indices, total_entries=len(entries))
        return 0 if not summary.failed else 1
    finally:
        summary.close()


# ──────────────────────────────────────────────────────────────────────────
# Restore — selective per-entry undo
#
# Differs from `cardo undo` (which reverses an entire run all-or-nothing) by
# letting the user pick individual entries from any past run's undo log. The
# typical use is "I organized 200 files; I want to put 5 of them back where
# they came from without disturbing the other 195".
#
# Selection methods:
#   - Interactive: paged listing + range syntax ("1-5, 8, 11-15", "a" for all)
#   - --range N-M: non-interactive, by entry index
#   - --grep PATTERN: non-interactive, by glob match against source-or-destination
#
# Restored entries are tracked in the log header's `undone_entries` list so
# they don't reappear in subsequent restore or undo runs. Once every entry
# in a log is consumed, the full `undone` flag flips to true.
# ──────────────────────────────────────────────────────────────────────────

def _entry_describe(entry: dict) -> str:
    """One-line human-readable summary of an undo log entry."""
    op = entry.get("op", "?")
    if op in ("move", "rename"):
        return f"{op:6}  {entry.get('to')} ← {entry.get('from')}"
    if op == "rmdir":
        return f"rmdir   {entry.get('path')}"
    return f"?       {entry!r}"


def _parse_range_spec(spec: str, max_n: int) -> list[int] | None:
    """Parse a selection like '1-5, 8, 11-15' into a sorted list of 0-based
    indices. Returns None on invalid syntax."""
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                a, b = chunk.split("-", 1)
                lo, hi = int(a), int(b)
            except ValueError:
                return None
            if lo > hi:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                if 1 <= i <= max_n:
                    out.add(i - 1)
        else:
            try:
                i = int(chunk)
            except ValueError:
                return None
            if 1 <= i <= max_n:
                out.add(i - 1)
    return sorted(out)


def _restore_resolve_log(args) -> tuple[Path, dict, list[dict]] | None:
    """Locate the undo log the user wants to operate on.

    Accepts a bare filename, a relative path, or an absolute path. Falls back
    to the most-recent-available log if no path given (matching `undo`).
    Returns (path, header, entries) or None if not found."""
    log_arg = getattr(args, "log_file", None)

    if log_arg:
        # Try a few interpretations of the argument.
        candidates = [
            Path(log_arg),
            Path(log_arg).expanduser(),
            UNDO_DIR / log_arg,
            UNDO_DIR / f"{log_arg}.jsonl",
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                parsed = _read_undo_log(p)
                if parsed is None:
                    print(f"✗ Could not parse undo log: {p}", file=sys.stderr)
                    return None
                return (p, parsed[0], parsed[1])
        print(f"✗ Undo log not found: {log_arg}", file=sys.stderr)
        print(f"  Tried: {', '.join(str(c) for c in candidates)}", file=sys.stderr)
        print(f"  Run `cardo undo --list` to see available logs.", file=sys.stderr)
        return None

    # No path given: use the most recent log that has anything pending.
    logs = find_recent_undo_logs()
    for path, header, entries in logs:
        if header.get("undone"):
            continue
        already = set(header.get("undone_entries", []))
        if any(i not in already for i in range(len(entries))):
            return (path, header, entries)

    print(f"  No undo logs with pending entries. Run `cardo undo --list` "
          f"to see history.", file=sys.stderr)
    return None


def _restore_interactive_pick(entries: list[dict],
                                already_done: set[int]) -> list[int] | None:
    """Show the entries and let the user select some by range syntax.
    Returns the list of 0-based indices to undo, or None if cancelled."""
    print()
    print(f"  Entries (✓ = already reversed by an earlier restore):")
    for i, entry in enumerate(entries, start=1):
        marker = "✓" if (i - 1) in already_done else " "
        print(f"  {marker} {i:>3}.  {_entry_describe(entry)}")
    print()
    print(f"  Pick entries to reverse. Examples:")
    print(f"    1-5, 8, 11-15      ranges and individual numbers")
    print(f"    a                  all pending entries")
    print(f"    q (or blank)       cancel")
    while True:
        try:
            spec = input("  Selection: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if spec in ("", "q", "quit"):
            return None
        if spec == "a":
            return [i for i in range(len(entries)) if i not in already_done]
        picked = _parse_range_spec(spec, max_n=len(entries))
        if picked is None:
            print("  Invalid selection. Try again, or 'q' to cancel.")
            continue
        if not picked:
            print("  Nothing selected. Try again, or 'q' to cancel.")
            continue
        # Filter out already-done entries silently.
        picked = [i for i in picked if i not in already_done]
        if not picked:
            print("  All selected entries were already reversed. Pick others, "
                  "or 'q' to cancel.")
            continue
        return picked


def _restore_grep_pick(entries: list[dict], pattern: str,
                        already_done: set[int]) -> list[int]:
    """Select entries whose source-or-destination path matches `pattern`."""
    picked: list[int] = []
    for i, entry in enumerate(entries):
        if i in already_done:
            continue
        # Try every plausible path field.
        for key in ("from", "to", "path"):
            val = entry.get(key)
            if val and fnmatch.fnmatch(str(val), pattern):
                picked.append(i)
                break
    return picked


def cmd_restore(args) -> int:
    if getattr(args, "list", False):
        return _list_undo_logs(args)

    target = _restore_resolve_log(args)
    if target is None:
        return 1
    path, header, entries = target

    if not entries:
        print(f"  Undo log has no recorded actions: {path}")
        return 0

    already_done: set[int] = set(header.get("undone_entries", []))
    pending_count = sum(1 for i in range(len(entries)) if i not in already_done)
    if pending_count == 0:
        print(f"  All entries in this log have already been reversed.")
        if not header.get("undone"):
            _mark_undo_log_consumed(path)
        return 0

    cmd_label = header.get("command", "?")
    argv_str = " ".join(shlex.quote(a) for a in header.get("argv", []))
    completed = header.get("completed") or "(incomplete)"

    print(f"  Restore from undo log:")
    print(f"    file:       {path.name}")
    print(f"    command:    cardo {cmd_label}")
    print(f"    argv:       {argv_str}")
    print(f"    completed:  {completed}")
    print(f"    entries:    {len(entries):,} total  ({pending_count:,} pending, "
          f"{len(already_done):,} already reversed)")

    # ─── Pick entries ────────────────────────────────────────────────
    if args.range_:
        picked = _parse_range_spec(args.range_, max_n=len(entries))
        if picked is None:
            print(f"✗ Invalid range: {args.range_!r}", file=sys.stderr)
            return 2
        picked = [i for i in picked if i not in already_done]
        if not picked:
            print(f"  Range matched nothing pending.")
            return 0
    elif args.grep:
        picked = _restore_grep_pick(entries, args.grep, already_done)
        if not picked:
            print(f"  No pending entries match pattern: {args.grep}")
            return 0
    else:
        picked_opt = _restore_interactive_pick(entries, already_done)
        if picked_opt is None:
            print("  Cancelled.")
            return 0
        picked = picked_opt

    print()
    print(f"  Will reverse {len(picked)} entr{'y' if len(picked) == 1 else 'ies'}:")
    for i in picked:
        print(f"    {_entry_describe(entries[i])}")
    print()

    if args.dry_run:
        print(f"  Dry run — no changes made.")
        return 0

    if not args.yes:
        if not confirm(f"  Proceed with {len(picked)} reversal(s)?"):
            print("  Cancelled.")
            return 0

    summary = RunSummary(f"restore-{cmd_label}", resolve_log_path(args))
    try:
        # Reverse the picked entries. We process in reverse-of-original-order
        # within the picked subset (same convention as `undo`) so dependent
        # ops untangle correctly.
        ordered = sorted(picked, reverse=True)
        succeeded: list[int] = []
        for idx in ordered:
            entry = entries[idx]
            ok, msg = _undo_one_entry(entry, args)
            if ok:
                summary.ok(msg)
                succeeded.append(idx)
                print(f"  ✓ {msg}")
            else:
                summary.fail(
                    f"{entry.get('op')}: "
                    f"{entry.get('to') or entry.get('path') or entry.get('from')}",
                    msg
                )
                sys.stderr.write(f"  ✗ {msg}\n")
        summary.print_summary()
        if succeeded:
            _mark_entries_consumed(path, succeeded, total_entries=len(entries))
        return 0 if not summary.failed else 1
    finally:
        summary.close()


# ──────────────────────────────────────────────────────────────────────────
# Verify
#
# Read-only integrity check: re-hash files and compare against the persistent
# hash cache (~/.cardo/cache/hashes.json). Detects:
#
#   • Silent corruption (file content changed but mtime/size match what the
#     cache stored — classic bit-rot / disk error symptoms)
#   • Modification (mtime or size differ AND content differs)
#   • Missing files (cache entry exists but file is gone)
#   • Untracked files (file present but no cache entry — optionally added so
#     future verify runs can check them)
#
# This shares the cache with `dedupe` — if you've ever dedupe'd a tree, you
# already have a baseline of expected hashes for verify to compare against.
# ──────────────────────────────────────────────────────────────────────────

def _cache_lookup_unchecked(cache: HashCache, path: Path) -> dict | None:
    """Return the raw cache entry for `path` without the size/mtime freshness
    check `cache.get()` does. Verify needs to compare hashes regardless of
    metadata — see module docstring for why."""
    with cache._lock:  # noqa: SLF001 — we own this lock contract
        return cache.data.get(str(path))


def _verify_one(path: Path, cache: HashCache) -> tuple[Path, str, str | None]:
    """Worker: hash one file and classify the result.

    Returns (path, status, detail). status is one of:
      'ok'           — hash matches cache
      'corrupted'    — hash differs AND mtime/size unchanged (silent corruption!)
      'modified'     — hash differs AND mtime/size changed (likely legitimate edit)
      'new'          — no cache entry; caller will decide whether to add
      'unreadable'   — could not hash the file
    `detail` is human-readable extra context for the report.
    """
    st = safe_stat(path)
    if st is None:
        return (path, "unreadable", "cannot stat")

    entry = _cache_lookup_unchecked(cache, path)
    try:
        new_hash = file_hash(path)
    except OSError as e:
        return (path, "unreadable", str(e))

    if entry is None:
        # Tell the caller the freshly-computed hash so it can store it if
        # --add-new is set.
        return (path, "new", new_hash)

    if entry.get("sha256") == new_hash:
        return (path, "ok", None)

    # Hash differs — distinguish between "legitimate edit" and "silent
    # corruption" by checking whether mtime/size match what we recorded.
    #
    # Mtime is stored with sub-second precision but many tools (touch -d,
    # rsync without --modify-window, FAT32/HFS+ filesystems) normalize to
    # whole seconds. Comparing strictly would mis-classify corruption as
    # modification whenever someone restored a file from a non-precise
    # source. We use a 2-second tolerance: tight enough to still catch
    # genuine edits, loose enough to absorb precision loss.
    same_size = entry.get("size") == st.st_size
    mtime_diff = abs((entry.get("mtime") or 0) - st.st_mtime)
    same_mtime = mtime_diff <= 2.0
    same_metadata = same_size and same_mtime
    if same_metadata:
        return (path, "corrupted",
                f"hash {entry.get('sha256','?')[:12]}… → {new_hash[:12]}… "
                f"(size & mtime unchanged)")
    return (path, "modified",
            f"hash differs and metadata changed since last seen")


def cmd_verify(args) -> int:
    root = Path(args.directory)
    if not _require_dir(root):
        return 1

    # ─── Preflight: estimate cost (full hashing is expensive) ───────────
    file_count, total_bytes = prescan_directory(root, recursive=args.recursive,
                                                 pattern=args.pattern)
    if file_count == 0:
        print("Nothing matched.")
        return 0
    if not preflight("hash", file_count, total_bytes,
                     assume_yes=args.yes, dry_run=False):
        return 0

    # ─── Load the cache ─────────────────────────────────────────────────
    cache = HashCache()
    if not cache.data and not args.add_new:
        print("  ! Cache is empty and --no-add-new was passed — nothing to "
              "verify against.")
        print("    Run `cardo dedupe <tree>` first, or omit --no-add-new to "
              "establish a baseline.")
        return 1
    if cache.data:
        print(f"  Cache loaded with {len(cache.data):,} previously seen file(s).")

    # ─── Hash everything in parallel (reusing dedupe's workers) ─────────
    workers = _resolve_workers(getattr(args, "workers", 0))
    files = list(iter_files(root, recursive=args.recursive, pattern=args.pattern))
    print(f"  Hashing {len(files):,} file(s) "
          f"{'(workers=' + str(workers) + ')' if workers > 1 else '(serial)'}…")
    bar = ProgressBar(len(files), total_bytes, label="Verify")

    results: list[tuple[Path, str, str | None]] = []
    if workers <= 1:
        for f in files:
            r = _verify_one(f, cache)
            results.append(r)
            st = safe_stat(f)
            bar.update(st.st_size if st else 0)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_verify_one, f, cache): f for f in files}
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                st = safe_stat(r[0])
                bar.update(st.st_size if st else 0)
    bar.finish()

    # ─── Detect orphans (in cache but missing from disk) ────────────────
    # We only consider cache entries under `root` — the cache may span many
    # trees and we don't want to report unrelated absences.
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    actually_present = {str(r[0]) for r in results}
    orphans: list[Path] = []
    for key in cache.data:
        try:
            key_path = Path(key)
            # Only report orphans inside the tree we're verifying.
            key_path.resolve().relative_to(root_resolved)
        except (ValueError, OSError):
            continue
        if key not in actually_present:
            orphans.append(Path(key))

    # ─── Classify + report ──────────────────────────────────────────────
    by_status: dict[str, list[tuple[Path, str | None]]] = defaultdict(list)
    for path, status, detail in results:
        by_status[status].append((path, detail))

    ok_count       = len(by_status["ok"])
    new_count      = len(by_status["new"])
    modified_count = len(by_status["modified"])
    corrupted      = by_status["corrupted"]
    unreadable     = by_status["unreadable"]

    # Populate cache with new files if requested.
    if args.add_new and new_count:
        for path, new_hash in by_status["new"]:
            if new_hash:
                cache.put(path, new_hash)

    # Always save the cache: even when --no-add-new, we don't strip anything,
    # so save is a no-op if there were no changes. But if --add-new added
    # entries, we want them persisted.
    if args.add_new:
        cache.save(prune=False)  # don't prune in verify — orphans are reported

    # ─── Output ─────────────────────────────────────────────────────────
    print()
    print(f"  ─── Verify results for {root} ───")
    print(f"    ✓ Unchanged:    {ok_count:,}")
    if new_count:
        verb = "added to cache" if args.add_new else "not in cache"
        print(f"    + New:          {new_count:,}  ({verb})")
    if modified_count:
        print(f"    ~ Modified:     {modified_count:,}  (size or mtime changed; expected for edited files)")
    if corrupted:
        print(f"    ⚠ CORRUPTED:    {len(corrupted):,}  "
              f"(content differs but metadata didn't — investigate!)")
    if unreadable:
        print(f"    ! Unreadable:   {len(unreadable):,}")
    if orphans:
        print(f"    – Missing:      {len(orphans):,}  "
              f"(in cache but no longer on disk)")

    # Always show details for the alarming categories.
    if corrupted:
        print()
        print(f"  ⚠ Files whose CONTENTS changed despite unchanged metadata:")
        print(f"  This usually indicates disk corruption, bit-rot, or an attack.")
        for path, detail in corrupted:
            print(f"      {path}")
            if detail:
                print(f"        {detail}")
    if unreadable:
        print()
        print(f"  Files that could not be hashed:")
        for path, detail in unreadable[:20]:
            print(f"      {path}  ({detail})")
        if len(unreadable) > 20:
            print(f"      … and {len(unreadable) - 20} more")
    if modified_count and args.show_modified:
        print()
        print(f"  Files modified since last cached:")
        for path, _ in by_status["modified"][:20]:
            print(f"      {path}")
        if modified_count > 20:
            print(f"      … and {modified_count - 20} more")
    if orphans and args.show_missing:
        print()
        print(f"  Files in cache but missing from disk:")
        for path in orphans[:20]:
            print(f"      {path}")
        if len(orphans) > 20:
            print(f"      … and {len(orphans) - 20} more")

    # ─── Optional HTML report ───────────────────────────────────────────
    if getattr(args, "report", False):
        # Each finding becomes a single-file "set" so the existing template
        # can render it. The 'kind' drives the colored stripe.
        report_sets: list[dict] = []
        for path, detail in corrupted:
            report_sets.append({
                "name": path.name, "size": 0, "count": 1,
                "paths": [str(path)], "kind": "duplicate",  # red stripe = bad
            })
        for path, _ in by_status["modified"]:
            report_sets.append({
                "name": path.name, "size": 0, "count": 1,
                "paths": [str(path)], "kind": "suspect",
            })
        for path in orphans:
            report_sets.append({
                "name": path.name, "size": 0, "count": 1,
                "paths": [str(path)], "kind": "advisory",
            })
        maybe_write_report(
            args, command="verify", root=root,
            summary={
                "total_checked": len(files),
                "unchanged": ok_count,
                "new": new_count,
                "modified": modified_count,
                "corrupted": len(corrupted),
                "unreadable": len(unreadable),
                "missing": len(orphans),
            },
            duplicate_sets=report_sets,
        )

    # ─── Exit code ──────────────────────────────────────────────────────
    # 0 = clean (no corruption, no unreadable)
    # 1 = something concerning — corruption OR unreadable files
    # We deliberately do NOT exit 1 just because there are "new" or "modified"
    # files — those are expected during normal work.
    if corrupted or unreadable:
        return 1
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Sync (one-way mirror)
#
# Copies src → dst so that dst reflects src. Three modes of "changed":
#   default — by mtime + size (rsync-style; fast, good enough for routine)
#   --checksum — by SHA-256 (definitive but reads every byte)
#
# Two policies for extras in dst (files dst has but src doesn't):
#   default       — leave them (--no-delete-extras semantics)
#   --mirror      — delete/trash them so dst exactly matches src
#
# This is STRICTLY ONE-WAY. Two-way sync requires conflict resolution we
# don't have. Don't point this at a directory that another tool also writes
# to expecting both sides to merge.
# ──────────────────────────────────────────────────────────────────────────

# Action types in the sync plan.
_SYNC_COPY    = "copy"     # new file: src has it, dst doesn't
_SYNC_UPDATE  = "update"   # exists in both, content/metadata differ
_SYNC_DELETE  = "delete"   # extra in dst, will be removed (only if --mirror)
_SYNC_SKIP    = "skip"     # exists in both, identical — no action


def _sync_compare(src_path: Path, dst_path: Path, *, use_hash: bool,
                   cache: HashCache | None) -> tuple[bool, str]:
    """Return (needs_update, reason). If True, dst should be replaced by src."""
    src_st = safe_stat(src_path)
    dst_st = safe_stat(dst_path)
    if src_st is None:
        return (False, "source unreadable")
    if dst_st is None:
        return (True, "destination unreadable — treating as missing")

    if use_hash:
        # Hash-based comparison: the gold standard. Slow but correct.
        try:
            src_hash = cached_file_hash(src_path, cache)
            dst_hash = cached_file_hash(dst_path, cache)
        except OSError as e:
            return (True, f"hash failed ({e}) — treating as different")
        if src_hash and dst_hash and src_hash == dst_hash:
            return (False, "hash matches")
        return (True, "content hash differs")

    # Fast path: mtime + size. Newer source OR different size → update.
    # Two-second mtime tolerance for the same reason verify uses it.
    if src_st.st_size != dst_st.st_size:
        return (True, f"size differs ({src_st.st_size} vs {dst_st.st_size})")
    mtime_diff = src_st.st_mtime - dst_st.st_mtime
    if mtime_diff > 2.0:
        return (True, f"source newer by {mtime_diff:.0f}s")
    if mtime_diff < -2.0:
        # Destination is newer. Don't touch it — that's not a sync, that's
        # destruction. Unless the user passed --force-older, we leave dst
        # alone with a warning at the end.
        return (False, f"destination is NEWER by {-mtime_diff:.0f}s (preserving)")
    return (False, "mtime+size match")


def _sync_build_plan(src: Path, dst: Path, args, cache: HashCache | None
                     ) -> tuple[list[tuple[str, Path, Path]], list[tuple[Path, str]]]:
    """Walk both trees and build the action plan.

    Returns (plan, warnings). Plan entries are (action, src_rel_path, dst_rel_path).
    The "skip" action is included so the summary can report it but isn't
    executed.
    """
    plan: list[tuple[str, Path, Path]] = []
    warnings: list[tuple[Path, str]] = []
    pattern = args.pattern

    # ─── Source side: build the set of files we expect in dst ─────────
    src_files: set[Path] = set()  # relative paths
    for src_file in iter_files(src, recursive=True, pattern=pattern):
        try:
            rel = src_file.relative_to(src)
        except ValueError:
            continue
        src_files.add(rel)
        dst_file = dst / rel
        if not dst_file.exists():
            plan.append((_SYNC_COPY, src_file, dst_file))
            continue
        if not dst_file.is_file():
            # dst slot is occupied by something else (a dir, a symlink to
            # a dir, etc.) — refuse to touch it.
            warnings.append((dst_file, "destination is not a regular file"))
            continue
        needs, reason = _sync_compare(src_file, dst_file,
                                       use_hash=args.checksum, cache=cache)
        if needs:
            plan.append((_SYNC_UPDATE, src_file, dst_file))
        else:
            plan.append((_SYNC_SKIP, src_file, dst_file))

    # ─── Destination side: find extras (only relevant for --mirror) ───
    if args.mirror and dst.exists():
        for dst_file in iter_files(dst, recursive=True, pattern=pattern):
            try:
                rel = dst_file.relative_to(dst)
            except ValueError:
                continue
            if rel not in src_files:
                # Bogus source path here — we won't use it for an extra
                # deletion, but the tuple shape needs something.
                plan.append((_SYNC_DELETE, src / rel, dst_file))

    return plan, warnings


def cmd_sync(args) -> int:
    src = Path(args.source).resolve()
    dst = Path(args.dest).resolve()

    if not src.exists():
        print(f"✗ Source does not exist: {src}", file=sys.stderr)
        return 1
    if not src.is_dir():
        print(f"✗ Source must be a directory: {src}", file=sys.stderr)
        return 1
    if src == dst:
        print(f"✗ Source and destination are the same: {src}", file=sys.stderr)
        return 1
    # Protect against the catastrophic --mirror typo where dst is an ancestor of
    # src (or vice versa) — would either delete the source or recurse forever.
    try:
        src.relative_to(dst)
        print(f"✗ Source is inside destination — refusing to sync.", file=sys.stderr)
        return 1
    except ValueError:
        pass
    try:
        dst.relative_to(src)
        print(f"✗ Destination is inside source — refusing to sync.", file=sys.stderr)
        return 1
    except ValueError:
        pass

    if not require_admin_if_needed("sync", [src, dst]):
        return 1

    use_trash, trash_err = trash_or_warn_if_requested(args)
    if trash_err:
        print(trash_err, file=sys.stderr)
        return 1

    # ─── Hash cache (only if --checksum) ─────────────────────────────
    cache: HashCache | None = None
    if args.checksum and not getattr(args, "no_cache", False):
        cache = HashCache()
        if cache.data:
            print(f"  Hash cache loaded with {len(cache.data):,} previously seen file(s).")

    # ─── Build the plan ──────────────────────────────────────────────
    print(f"  Comparing {src} → {dst}…")
    plan, warnings = _sync_build_plan(src, dst, args, cache)

    if warnings:
        print()
        print(f"  ⚠ {len(warnings)} path(s) skipped due to type mismatch:")
        for path, reason in warnings[:5]:
            print(f"      {path}  ({reason})")
        if len(warnings) > 5:
            print(f"      … and {len(warnings) - 5} more")

    copies   = [e for e in plan if e[0] == _SYNC_COPY]
    updates  = [e for e in plan if e[0] == _SYNC_UPDATE]
    deletes  = [e for e in plan if e[0] == _SYNC_DELETE]
    skips    = [e for e in plan if e[0] == _SYNC_SKIP]

    # Bytes to copy = new files + updated files (source side).
    total_bytes = 0
    for _, src_file, _ in copies + updates:
        st = safe_stat(src_file)
        if st:
            total_bytes += st.st_size

    print()
    print(f"  ─── Sync plan ───")
    print(f"    Copy new:     {len(copies):,}")
    print(f"    Update:       {len(updates):,}")
    print(f"    Up-to-date:   {len(skips):,}")
    if args.mirror:
        verb = "Trash" if use_trash else "Delete"
        print(f"    {verb} extras: {len(deletes):,}  (in dst but not in src)")
    elif deletes:  # only happens if we set --mirror; defensive
        print(f"    Extras (ignored): {len(deletes):,}")
    print(f"    Total to transfer: {human_size(total_bytes)}")

    if not (copies or updates or deletes):
        print("\n  Nothing to do — destination is already in sync.")
        return 0

    # ─── Protection preflight ────────────────────────────────────────
    # Same protection layer as the other destructive commands. Check both
    # sides: don't read FROM nor write INTO installed-application content.
    actionable_plan = copies + updates + deletes
    if not getattr(args, "include_unsafe", False):
        src_install, src_warn = detect_install_folders_with_root_check(src, recursive=True)
        dst_install_set: set[Path] = set()
        dst_warn: list[str] = []
        if dst.exists() and dst.is_dir():
            dst_install_set, dst_warn = detect_install_folders_with_root_check(
                dst, recursive=True
            )
        all_install = src_install | dst_install_set
        root_warnings = src_warn + dst_warn

        def _entry_protected(entry: tuple[str, Path, Path]) -> tuple[bool, str | None]:
            action, src_p, dst_p = entry
            src_prot, src_reason = is_protected_path(src_p, all_install)
            if src_prot:
                return (True, f"source {src_reason}")
            dst_prot, dst_reason = is_protected_path(dst_p.parent, all_install)
            if dst_prot:
                return (True, f"destination {dst_reason}")
            return (False, None)

        safe: list[tuple[str, Path, Path]] = []
        protected: list[tuple[tuple[str, Path, Path], str]] = []
        for entry in actionable_plan:
            is_prot, reason = _entry_protected(entry)
            if is_prot:
                protected.append((entry, reason or "protected"))
            else:
                safe.append(entry)

        if protected or root_warnings:
            if not confirm_protection_skip(
                "sync",
                safe_count=len(safe),
                protected=protected,
                path_of=lambda entry: entry[1],
                assume_yes=args.yes,
                dry_run=args.dry_run,
                root_warnings=root_warnings,
            ):
                return 0
        actionable_plan = safe
        copies  = [e for e in safe if e[0] == _SYNC_COPY]
        updates = [e for e in safe if e[0] == _SYNC_UPDATE]
        deletes = [e for e in safe if e[0] == _SYNC_DELETE]
        total_bytes = sum(
            (st.st_size if (st := safe_stat(s)) else 0)
            for _, s, _ in copies + updates
        )

    # ─── Time-estimate preflight ─────────────────────────────────────
    total_files = len(copies) + len(updates) + len(deletes)
    if not preflight("copy", total_files, total_bytes,
                     assume_yes=args.yes, dry_run=args.dry_run):
        return 0

    # ─── Dry-run output ──────────────────────────────────────────────
    if args.dry_run:
        for _, src_p, dst_p in copies:
            print(f"  Would copy:   {src_p} → {dst_p}")
        for _, src_p, dst_p in updates:
            print(f"  Would update: {src_p} → {dst_p}")
        if args.mirror:
            verb = "trash" if use_trash else "delete"
            for _, _, dst_p in deletes:
                print(f"  Would {verb}: {dst_p}")
        return 0

    # ─── Execute ─────────────────────────────────────────────────────
    if use_trash and deletes:
        print(f"  Trash mode: extras go to the OS trash (recoverable).")

    summary = RunSummary("sync", resolve_log_path(args))
    maybe_attach_undo(summary, args)
    try:
        bar = ProgressBar(total_files, total_bytes, label="Syncing")

        # Copy + update.
        for action, src_p, dst_p in copies + updates:
            size = (st.st_size if (st := safe_stat(src_p)) else 0)
            try:
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                if args.follow_symlinks:
                    shutil.copy2(src_p, dst_p, follow_symlinks=True)
                else:
                    shutil.copy2(src_p, dst_p, follow_symlinks=False)
                # The undo for a copy/update is "delete the new dst copy".
                # But for an UPDATE we'd be deleting a file that previously
                # existed with different content — that's not really an undo,
                # it's destruction. So we only record COPY actions, not UPDATEs.
                if action == _SYNC_COPY:
                    summary.ok(f"copied {src_p} → {dst_p}")
                else:
                    summary.ok(f"updated {dst_p} from {src_p}")
            except OSError as e:
                summary.fail(f"{src_p} → {dst_p}", str(e))
                sys.stderr.write(f"\n  ! {action} failed: {e}\n")
            bar.update(size)

        # Delete extras (only if --mirror).
        for action, _, dst_p in deletes:
            try:
                if use_trash:
                    ok, err = _send_to_trash(dst_p)
                    if ok:
                        summary.ok(f"trashed extra {dst_p}")
                    else:
                        summary.fail(str(dst_p), err or "trash failed")
                        sys.stderr.write(f"\n  ! Trash failed: {err}\n")
                else:
                    dst_p.unlink()
                    summary.ok(f"deleted extra {dst_p}")
            except OSError as e:
                summary.fail(str(dst_p), str(e))
                sys.stderr.write(f"\n  ! Delete failed: {e}\n")
            bar.update(0)
        bar.finish()
        summary.print_summary()
        return 0 if not summary.failed else 1
    finally:
        summary.close()


# ──────────────────────────────────────────────────────────────────────────
# CLI plumbing
# ──────────────────────────────────────────────────────────────────────────

def _add_common(sp, *, with_pattern: bool = True, with_recursive: bool = True) -> None:
    """Flags shared by most write-style commands."""
    if with_pattern:
        sp.add_argument("-p", "--pattern", help="Glob filter, e.g. '*.jpg'")
    if with_recursive:
        sp.add_argument("-r", "--recursive", action="store_true",
                        help="Descend into subdirectories")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="Preview without changing anything")
    sp.add_argument("-i", "--interactive", action="store_true",
                    help="Confirm each file")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the time-estimate confirmation prompt")
    sp.add_argument("--log", nargs="?", const="", default=None, metavar="PATH",
                    help="Write a log of every action. With no value, logs to ~/.cardo/logs/")
    sp.add_argument("--no-undo", action="store_true",
                    help="Skip writing the undo log for this run. Use when you "
                         "definitely won't want to reverse the operation later.")
    sp.add_argument("--include-unsafe", action="store_true",
                    help="Bypass the installation-folder protection check. "
                         "NOT recommended — destructive ops inside .app bundles, "
                         "Adobe/Maxon-style folders etc. can break installed software.")


def _add_yes_and_report(sp) -> None:
    """For read-only commands that don't take --dry-run/--interactive/--log."""
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the time-estimate confirmation prompt")
    sp.add_argument("--report", action="store_true",
                    help="Save an HTML report of the findings to ~/.cardo/reports/.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cardo",
        description="A friendly command-line file manager.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # copy
    sp = sub.add_parser("copy", help="Copy files to a destination")
    sp.add_argument("source")
    sp.add_argument("dest")
    sp.add_argument("--overwrite", action="store_true",
                    help="Overwrite existing files (default: rename)")
    _add_common(sp)
    sp.set_defaults(func=cmd_copy)

    # move
    sp = sub.add_parser("move", help="Move files to a destination")
    sp.add_argument("source")
    sp.add_argument("dest")
    sp.add_argument("--overwrite", action="store_true")
    _add_common(sp)
    sp.set_defaults(func=cmd_move)

    # rename
    sp = sub.add_parser("rename", help="Bulk-rename files")
    sp.add_argument("directory")
    sp.add_argument("--regex", nargs=2, metavar=("PATTERN", "REPLACEMENT"),
                    help="Apply regex substitution to the stem")
    sp.add_argument("--prefix", help="Prepend to stem")
    sp.add_argument("--suffix", help="Append to stem")
    sp.add_argument("--lower", action="store_true", help="Lowercase the stem")
    sp.add_argument("--upper", action="store_true", help="Uppercase the stem")
    sp.add_argument("--numbered", metavar="TEMPLATE",
                    help="Replace stem with a numbered template, e.g. 'photo_{:03d}'")
    sp.add_argument("--start", type=int, default=1,
                    help="Starting number for --numbered (default 1)")
    sp.add_argument("--ext", help="Replace extension (empty string to strip)")
    sp.add_argument("--overwrite", action="store_true")
    _add_common(sp)
    sp.set_defaults(func=cmd_rename)

    # dedupe
    sp = sub.add_parser("dedupe", help="Find and remove duplicates by content hash")
    sp.add_argument("directory")
    sp.add_argument("--mode", choices=["quick", "standard", "paranoid"],
                    default="standard",
                    help="Scan mode. 'quick' = metadata-only triage (no hashing, "
                         "no deletion). 'standard' = staged SHA-256 (default). "
                         "'paranoid' = staged SHA-256 + byte-by-byte verification "
                         "of every deletion before it happens.")
    sp.add_argument("--min-size", type=int, default=4, metavar="KB",
                    help="Skip files smaller than this many KB (default: 4 KB). "
                         "Small files clog up the count but reclaim very little space.")
    sp.add_argument("--include-empty", action="store_true",
                    help="Include 0-byte files (skipped by default).")
    sp.add_argument("--no-cache", action="store_true",
                    help="Disable the persistent hash cache (~/.cardo/cache/).")
    sp.add_argument("--report", action="store_true",
                    help="Write an HTML report to ~/.cardo/reports/ at the end.")
    sp.add_argument("--workers", type=int, default=0, metavar="N",
                    help="Parallel hashing workers (0 = auto: min(8, CPU count); "
                         "1 = serial). Hashing parallelizes well on modern SSDs.")
    sp.add_argument("--trash", action="store_true",
                    help="Send duplicates to the OS trash instead of unlinking. "
                         "Requires the `send2trash` package.")
    # Note: --include-unsafe is added by _add_common(sp) below.
    _add_common(sp)
    sp.set_defaults(func=cmd_dedupe)

    # name-clash
    sp = sub.add_parser("name-clash", help="Report files sharing a name across the tree (read-only)")
    sp.add_argument("directory")
    sp.add_argument("-p", "--pattern", help="Glob filter, e.g. '*.jpg'")
    sp.add_argument("--ignore-ext", action="store_true",
                    help="Match on stem only, ignoring extension (e.g. 'photo.jpg' vs 'photo.png')")
    sp.add_argument("--ignore-case", action="store_true",
                    help="Treat 'Photo.JPG' and 'photo.jpg' as the same name")
    _add_yes_and_report(sp)
    sp.set_defaults(func=cmd_name_clash)

    # organize
    sp = sub.add_parser("organize", help="Sort files into category subfolders")
    sp.add_argument("directory")
    _add_common(sp)
    sp.set_defaults(func=cmd_organize)

    # search
    sp = sub.add_parser("search", help="Find files by name, size, or age")
    sp.add_argument("directory")
    sp.add_argument("-p", "--pattern", help="Glob filter, e.g. '*.log'")
    sp.add_argument("--ext", help="Filter by extension")
    sp.add_argument("--min-size", type=int, help="Minimum size in KB")
    sp.add_argument("--max-size", type=int, help="Maximum size in KB")
    sp.add_argument("--newer-than", type=float, metavar="DAYS",
                    help="Modified within N days")
    sp.add_argument("--older-than", type=float, metavar="DAYS",
                    help="Not modified for N days")
    _add_yes_and_report(sp)
    sp.set_defaults(func=cmd_search)

    # tree
    sp = sub.add_parser("tree", help="Print a directory tree")
    sp.add_argument("directory", nargs="?", default=".")
    sp.add_argument("--max-depth", type=int, help="Limit recursion depth")
    _add_yes_and_report(sp)
    sp.set_defaults(func=cmd_tree)

    # clean
    sp = sub.add_parser("clean", help="Remove empty subdirectories")
    sp.add_argument("directory")
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the time-estimate confirmation prompt")
    sp.add_argument("--trash", action="store_true",
                    help="Send empty directories to the OS trash instead of "
                         "rmdir'ing them. Requires the `send2trash` package.")
    sp.add_argument("--log", nargs="?", const="", default=None, metavar="PATH",
                    help="Write a log of every action. With no value, logs to ~/.cardo/logs/")
    sp.add_argument("--no-undo", action="store_true",
                    help="Skip writing the undo log for this run.")
    sp.add_argument("--include-unsafe", action="store_true",
                    help="Bypass the installation-folder protection check. "
                         "NOT recommended — empty subdirs of .app bundles, "
                         "Adobe/Maxon-style folders, etc. will be removed.")
    sp.set_defaults(func=cmd_clean)

    # stats
    sp = sub.add_parser("stats", help="Show a size/count breakdown by category")
    sp.add_argument("directory")
    _add_yes_and_report(sp)
    sp.set_defaults(func=cmd_stats)

    # config — manage the ~/.cardo/config.toml file
    sp = sub.add_parser("config", help="Show, locate, or initialize the config file")
    sub_cfg = sp.add_subparsers(dest="config_action", required=True)

    sub_cfg.add_parser("show", help="Print effective settings and source")
    sub_cfg.add_parser("path", help="Print the config file path")
    init_sp = sub_cfg.add_parser("init", help="Write a starter config file")
    init_sp.add_argument("--force", action="store_true",
                         help="Overwrite an existing config file")
    sp.set_defaults(func=cmd_config)

    # undo — reverse the most recent reversible run
    sp = sub.add_parser("undo",
                        help="Reverse the most recent move/rename/organize/clean run")
    sp.add_argument("--list", action="store_true",
                    help="Show recent undo logs without doing anything")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="Show what would be reversed without changing anything")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt")
    sp.add_argument("--force", action="store_true",
                    help="Overwrite destinations that already exist when reversing")
    sp.add_argument("--log", nargs="?", const="", default=None, metavar="PATH",
                    help="Write a human-readable log of the undo run")
    sp.set_defaults(func=cmd_undo)

    # restore — selective per-entry undo
    sp = sub.add_parser("restore",
                        help="Selectively reverse individual entries from any "
                             "past run's undo log")
    sp.add_argument("log_file", nargs="?", default=None,
                    help="Undo log filename (in ~/.cardo/undo/) or full path. "
                         "If omitted, uses the most recent pending log.")
    sp.add_argument("--list", action="store_true",
                    help="Show recent undo logs without doing anything")
    sp.add_argument("--range", dest="range_", metavar="SPEC",
                    help="Select entries by range, e.g. '1-5, 8, 11-15'. "
                         "Skips the interactive picker.")
    sp.add_argument("--grep", metavar="PATTERN",
                    help="Select entries whose source or destination path "
                         "matches a glob pattern (e.g. '*.jpg'). Skips the "
                         "interactive picker.")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="Show what would be reversed without changing anything")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt")
    sp.add_argument("--force", action="store_true",
                    help="Overwrite destinations that already exist when reversing")
    sp.add_argument("--log", nargs="?", const="", default=None, metavar="PATH",
                    help="Write a human-readable log of the restore run")
    sp.set_defaults(func=cmd_restore)

    # verify — re-hash files and compare against the persistent cache
    sp = sub.add_parser("verify",
                        help="Re-hash files and compare against the persistent "
                             "hash cache (detect bit-rot / corruption)")
    sp.add_argument("directory")
    sp.add_argument("-r", "--recursive", action="store_true",
                    help="Descend into subdirectories")
    sp.add_argument("-p", "--pattern",
                    help="Glob filter, e.g. '*.tif'")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the time-estimate confirmation prompt")
    sp.add_argument("--workers", type=int, default=0, metavar="N",
                    help="Parallel hashing workers (0 = auto: min(8, CPU count); "
                         "1 = serial). Same model as dedupe.")
    sp.add_argument("--no-add-new", dest="add_new", action="store_false",
                    default=True,
                    help="Don't add untracked files to the cache. Default is to "
                         "add them so the next verify run can check them.")
    sp.add_argument("--show-modified", action="store_true",
                    help="List the paths of modified files in the report "
                         "(default: just count them).")
    sp.add_argument("--show-missing", action="store_true",
                    help="List the paths of missing (orphaned) files in the report.")
    sp.add_argument("--report", action="store_true",
                    help="Write an HTML report to ~/.cardo/reports/.")
    sp.set_defaults(func=cmd_verify)

    # sync — one-way mirror src → dst
    sp = sub.add_parser("sync",
                        help="One-way mirror: make destination match source")
    sp.add_argument("source")
    sp.add_argument("dest")
    sp.add_argument("-p", "--pattern", help="Glob filter, e.g. '*.jpg'")
    sp.add_argument("-c", "--checksum", action="store_true",
                    help="Compare by SHA-256 content hash instead of mtime+size. "
                         "Definitive but reads every byte.")
    sp.add_argument("--mirror", action="store_true",
                    help="Also delete files in destination that aren't in source "
                         "(so dst exactly mirrors src). Without this, sync only "
                         "adds new files and updates existing ones.")
    sp.add_argument("--trash", action="store_true",
                    help="When --mirror deletes extras, send them to the OS "
                         "trash instead of unlinking permanently. Requires "
                         "the `send2trash` package.")
    sp.add_argument("--follow-symlinks", action="store_true",
                    help="Dereference symlinks in the source instead of "
                         "copying them as symlinks.")
    sp.add_argument("--no-cache", action="store_true",
                    help="Disable the persistent hash cache (only relevant "
                         "with --checksum).")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="Preview without changing anything")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Skip the time-estimate confirmation prompt")
    sp.add_argument("--log", nargs="?", const="", default=None, metavar="PATH",
                    help="Write a log of every action. With no value, logs to "
                         "~/.cardo/logs/")
    sp.add_argument("--include-unsafe", action="store_true",
                    help="Bypass the installation-folder protection check. "
                         "NOT recommended.")
    sp.set_defaults(func=cmd_sync)

    return p


def _detect_explicit_flags(argv: list[str] | None) -> set[str]:
    """Return the set of long-form flag names that appear in argv.

    Used so we can distinguish "user passed --mode standard" from "user
    didn't pass --mode and argparse filled in the default standard". The
    distinction matters when a config file wants to override the default.

    We deliberately keep this simple — exact long-form matching. Short
    flags and `--flag=value` are both handled.
    """
    if argv is None:
        argv = sys.argv[1:]
    found: set[str] = set()
    for tok in argv:
        if not tok.startswith("--"):
            continue
        # --flag=value or --flag — normalize to bare name
        name = tok.split("=", 1)[0]
        found.add(name)
    return found


def main(argv: list[str] | None = None) -> int:
    # Load config first; this affects defaults but never overrides explicit CLI flags.
    global CONFIG
    CONFIG = load_config()
    apply_config_to_globals(CONFIG)

    explicit_flags = _detect_explicit_flags(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    # Record per-flag explicitness for the apply step. We only need flags
    # whose config-vs-CLI precedence is ambiguous because argparse fills a
    # non-None default.
    args._cli_mode_explicit = "--mode" in explicit_flags
    args._cli_min_size_explicit = "--min-size" in explicit_flags

    # Skip config-default application for `config` itself — it has its own flow
    # and reads CONFIG directly. Same for any command without a func attribute.
    if getattr(args, "command", None) not in (None, "config"):
        _apply_config_defaults(args, CONFIG)
        # Visibility: if assume_yes is being forced from config, tell the user
        # once so they don't wonder why the "Proceed?" prompt is missing.
        if CONFIG.assume_yes and CONFIG.overrides.get("defaults.assume_yes") is True:
            user_passed_yes = (
                "--yes" in explicit_flags
                or any(a == "-y" or (a.startswith("-") and not a.startswith("--") and "y" in a[1:])
                       for a in (argv if argv is not None else sys.argv[1:]))
            )
            if not user_passed_yes:
                print("  (config: assume_yes=true — skipping confirmations)",
                      file=sys.stderr)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # User piped to head/less and closed early — exit silently.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
