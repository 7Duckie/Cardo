# Cardo

> A command-line file manager that asks before it breaks things.

Cardo helps you organize, deduplicate, verify, and tidy up files from the
terminal — with safety rails. Every destructive operation previews what
it'll do, refuses to touch installed-application content by default, and
writes an undo log you can reverse later.

[![CI](https://github.com/YOUR-USERNAME/cardo/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR-USERNAME/cardo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## 60-second tour

Install:

```bash
pip install git+https://github.com/7Duckie/cardo.git
```

See what's in a folder:

```bash
cardo stats ~/Downloads
```

Preview organizing it (no changes made):

```bash
cardo organize ~/Downloads -n
```

If the preview looks right, run it for real:

```bash
cardo organize ~/Downloads
```

Made a mistake?

```bash
cardo undo
```

That's the whole loop: preview → do → undo. Every destructive command
follows it.

---

## What it does

Cardo is one binary with fifteen subcommands for the things you do with files
that aren't quite worth opening a real file manager for:

| Read-only | Reshape | Verify | Recovery |
| :--- | :--- | :--- | :--- |
| `stats` | `copy` / `move` | `verify` | `undo` |
| `tree` | `rename` | `dedupe` | `restore` |
| `search` | `organize` | | |
| `name-clash` | `clean` | | |
| | `sync` | | |
| | `config` | | |

It does these things with three opinions running through every command:

1. **Tell the user what will happen before it happens.** Plans, time
   estimates, file counts, and dry-run mode are not afterthoughts; they're
   how every destructive command works.
2. **Refuse to touch installed applications by default.** Cardo detects
   Adobe-style folders, `.app` bundles, vendor installations and similar
   structures, and protects them with a confirmation prompt — so you don't
   accidentally remove the empty preset folders inside Cinema 4D and have to
   reinstall.
3. **Make actions reversible when reasonable.** Every move / rename / organize
   / clean run writes a structured undo log. `cardo undo` reverses the last
   one; `cardo restore` lets you cherry-pick individual entries.

---

## Quick start

```bash
# Install from GitHub
pip install git+https://github.com/7Duckie/cardo.git

# Or, if you'd rather have a single self-contained script:
curl -O https://raw.githubusercontent.com/7Duckie/cardo/main/cardo.py
chmod +x cardo.py

# Try it out
cardo stats ~/Downloads
cardo dedupe ~/Pictures -r --report           # find duplicates; HTML report
cardo organize ~/Downloads -r --dry-run       # preview before committing
cardo clean ~/Desktop -y --trash              # remove empty dirs to trash
cardo undo                                    # reverse the last operation
```

The optional `send2trash` package gives you `--trash` support on every
destructive command. Without it cardo still works — it just unlinks files
permanently when asked to delete them. Strongly recommended:

```bash
pip install send2trash
```

---

## Why this exists

Most filesystem cleanup tools fall into two camps. **Specialized utilities**
(rdfind, fdupes, rmlint, fsync, custom rsync wrappers) do one thing each, with
inconsistent UX between them. **GUI file managers** are fine for browsing but
clumsy for batch operations and impossible to script.

Cardo is the middle path: a single CLI, consistent UX across operations,
batch-shaped, scriptable, and with a level of caution about destructive
actions that the alternatives tend not to bother with.

The protection layer in particular came from real damage. An early version
of cardo (then called `filemgr`) was pointed at the top of a working SSD and
asked to remove empty directories. It happily started trashing the empty
preset folders inside Adobe InDesign 2026, Cinema 4D 2024, and Motion.app
— because empty directories are empty directories. The current version
recognizes installed-application structure and refuses to touch it without
explicit override. That experience set the design philosophy for everything
since.

---

## Commands at a glance

Run `cardo <command> --help` for full details. Quick reference:

```
cardo stats DIR              Size/count breakdown by category
cardo tree DIR               Directory tree, optionally depth-limited
cardo search DIR ...         Find by name / size / age
cardo name-clash DIR         Files with identical names across the tree

cardo copy SRC DST           Copy files, optionally recursive
cardo move SRC DST           Move files (writes undo log)
cardo rename DIR ...         Bulk rename with regex / prefix / suffix / etc.
cardo organize DIR           Sort files into category subfolders (Images/, …)
cardo dedupe DIR             Find/remove duplicate files (three modes)
cardo clean DIR              Remove empty subdirectories
cardo sync SRC DST           One-way mirror (rsync-style)

cardo verify DIR             Re-hash files and compare to cache (bit-rot detection)
cardo undo                   Reverse the most recent reversible run
cardo restore [LOG]          Selectively reverse entries from any past run
cardo config                 Show, locate, or initialize the config file
```

All destructive commands support:

```
-n, --dry-run         Preview without changing anything
-y, --yes             Skip the time-estimate confirmation prompt
-r, --recursive       Descend into subdirectories
-p, --pattern GLOB    Filter to matching files only
--trash               Send removed files to the OS trash (recoverable)
--include-unsafe      Bypass installation-folder protection (NOT recommended)
--no-undo             Skip writing the undo log for this run
--log [PATH]          Write a human-readable log of every action
```

---

## A tour of the safety features

### Time-estimate preflight

Before any destructive command runs, cardo prints what it's about to do and
roughly how long it will take, then asks for confirmation:

```
  Plan: 1,247 file(s), 8.3 GB total
  Estimated time for dedupe (hash): ~1m 12s
  Proceed? [y/N]
```

For tiny operations the prompt is skipped automatically; for big ones you
get a moment to bail.

### Installation-folder protection

Cardo recognizes installed applications by several signals (path suffixes
like `.app`/`.framework`/`.lrdata`/`.photoslibrary`, vendor folder names
like "Adobe", "Maxon Cinema 4D", JetBrains-style layouts, reverse-DNS
patterns) and refuses to operate inside them without confirmation:

```
  ⚠ Protection: skipping 8 action(s) that would touch installed-application
    content:
         4× inside installation folder 'Adobe InDesign 2026'
         2× inside installation folder 'Maxon Cinema 4D 2024'
         2× inside .app package

    First 5 of 8:
      • /Volumes/SSD/Adobe InDesign 2026/Plug-Ins
      ...

  clean will proceed with 1,239 safe action(s) and skip the 8 protected one(s).
  Proceed? [y/N]
```

Set `--include-unsafe` to override. Don't do that unless you're certain.

### Trash mode

Pass `--trash` to dedupe, clean, sync, or rename-equivalent ops, and removed
files go to the OS Trash instead of being unlinked. Works on macOS,
Windows, and most Linux desktops via the `send2trash` package.

### Dry-run everywhere

`-n` or `--dry-run` shows exactly what would happen without touching disk.
The output uses the same format as the real run so you can read it the
same way.

### Undo and restore

`cardo undo` reverses the most recent reversible run (move, rename, organize,
clean). `cardo restore` lets you pick individual entries from any past run
to roll back:

```bash
cardo restore --range "5-10, 15"        # by entry number
cardo restore --grep "*.jpg"            # by glob
cardo restore                            # interactive picker
cardo undo --list                        # show all reversible runs
```

Partial consumption is tracked — you can keep restoring entries from the
same log until they're all reversed.

### Verify (bit-rot detection)

Long-term storage degrades silently. `cardo verify` re-hashes files and
compares against the persistent hash cache (the same one `dedupe` uses).
It distinguishes:

- **Unchanged**: hash matches cached value
- **Modified**: hash differs and metadata changed (normal edit)
- **Corrupted**: hash differs but metadata didn't (silent corruption — investigate!)
- **Missing**: cache entry exists but file is gone
- **New**: file present, not in cache (optionally added so future runs can check it)

Run it on a cron / scheduled task for archive folders that should never
change.

---

## Configuration

Optional config file at `~/.cardo/config.toml`. Run `cardo config init` to
write a commented starter. Example:

```toml
[defaults]
trash       = true       # destructive ops default to --trash
assume_yes  = false      # never skip prompts (overridable per-command)
report      = false      # don't auto-write HTML reports

[dedupe]
mode        = "standard"
min_size    = 4          # KB; files smaller than this aren't dedupe candidates
workers     = 0          # 0 = auto: min(8, CPU count)

[categories]
# Custom file-type categories for `organize` and `stats`
"3d-assets" = [".blend", ".fbx", ".obj", ".usd", ".usdc", ".usdz"]
"raw-photos" = [".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng"]
```

CLI flags always override config values. See
[docs/commands.md](docs/commands.md#config) for the full reference, or
run `cardo config init` to write a fully-commented starter file.

---

## Ideas: what cardo is good for, and what you could build with it

### As a personal tool

- **Keep Downloads tidy.** `cardo organize ~/Downloads -r --dry-run`, then
  run for real.
- **Find what's eating your disk.** `cardo stats ~ -r` for a category
  breakdown, then drill in with `cardo search` or `cardo dedupe`.
- **Recover from photo-library imports gone wrong.** `cardo name-clash`
  finds duplicate-named files spread across your tree.
- **Detect bit-rot on archive drives.** Schedule `cardo verify` weekly on
  important folders; exit code 1 means "something is wrong, investigate".

### As part of a backup workflow

- `cardo sync` for the actual copying. It's not as fast as rsync but it's
  consistent with the rest of cardo's UX, runs everywhere Python runs, and
  honors the protection layer.
- `cardo dedupe` on the backup destination before the next sync, to keep
  the backup tree from accumulating duplicates that the original source
  didn't have.
- `cardo verify` after the sync, against a cache populated on the source,
  to detect corruption introduced by the transit.

### As a building block in larger scripts

Every command:
- Returns a meaningful exit code (`0` clean, `1` something went wrong, `2`
  invalid arguments)
- Emits structured JSONL undo logs at `~/.cardo/undo/`
- Supports `--report` to write a self-contained HTML summary you can email,
  archive, or display in a dashboard
- Has a stable `--help` surface so you can grep through behavior

For pipelines that need finer-grained machine output, the JSONL undo files
are the most reliable place to read from — they're a near-complete account
of what each run did.

### As a teaching artifact

The cardo source is one Python file (~4,500 lines) deliberately written to
be readable. Each section has a header comment explaining what it does and
why. It's a self-contained example of:

- Separating "build the plan" from "execute the plan" in destructive
  commands, so the user can see what would happen before it happens
- Thread-safe progress reporting + caching for parallel hashing
- Lifting cross-cutting safety logic (protection, trash, undo) into shared
  helpers that every command can call uniformly
- A real-world structured-log format (the undo JSONL) used both for
  human display (`undo --list`) and machine action (`restore --range`)

You can read it linearly. The order is roughly: utilities → safety layer →
hashing → run summary / logging / undo → individual commands → CLI plumbing
→ main.

### As ideas to extend

Cardo's design treats the filesystem as a structured space that humans
need help navigating. Natural directions to take it:

- **Tags / virtual folders.** Make `~/Photos/2024/Family/Beach` look like
  `~/Photos/tags/Family/2024/Beach` to the user, via a tag-overlay layer.
- **Periodic snapshot management.** Wrap APFS or ZFS snapshots with the
  same time-estimate / protection / undo discipline cardo applies to
  file-level operations.
- **Cross-device deduplication.** `dedupe` currently works on a single
  tree. A `dedupe-across-devices` mode using the hash cache as the
  rendezvous point could find duplicates between your laptop and a NAS
  without copying the files first.
- **Quota-style "burn-down" cleanup.** "Get this folder under 100 GB,
  starting with the largest least-recently-accessed files." A planner +
  the existing dry-run mechanism + the protection layer ≈ a safer
  duplicate of what cleanup tools attempt today.
- **A daemon mode.** Run cardo in the background watching specific folders
  (Downloads, Desktop) and surface suggestions: "you have 4 GB of installer
  .dmg files older than 90 days — clean?"

The point of releasing cardo as a single readable Python file under MIT is
that any of these are reasonable forks or downstream projects. The
protection layer in particular is reusable for anything that mutates a
filesystem on behalf of a user.

---

## Installation

See [docs/installation.md](docs/installation.md) for full details. Short
version:

```bash
# As a package, from GitHub
pip install git+https://github.com/7Duckie/cardo.git

# Or download the script and run directly
curl -O https://raw.githubusercontent.com/7Duckie/cardo/main/cardo.py
python3 cardo.py --help
```

Requires Python 3.11+ (for `tomllib`). The `send2trash` package is
optional but recommended for trash support.

---

## Documentation

| Document | What's in it |
| :--- | :--- |
| [docs/installation.md](docs/installation.md) | All the installation paths |
| [docs/commands.md](docs/commands.md) | Every command, every flag, examples |
| [docs/safety.md](docs/safety.md) | The protection system, trash, undo — the *why* |
| [docs/design-notes.md](docs/design-notes.md) | Decisions and tradeoffs |

Config file reference is included in [docs/commands.md](docs/commands.md)
under the `config` command, and the `cardo config init` command writes a
fully-commented starter file.

---

## Contributing

Bug reports and patches welcome — open an issue or PR on GitHub. For
non-trivial changes, please open an issue first to discuss the direction.
Be respectful, be patient, and assume good faith from others.

---

## License

[MIT](LICENSE). Use it commercially, fork it, vendor it into your own tools,
build a SaaS around it — all fine. Just keep the copyright notice if you
redistribute.
