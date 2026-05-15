# Commands

Full reference for every cardo subcommand. For installation see
[installation.md](installation.md); for the config file see
[commands.md](commands.md#config); for the safety layer see [safety.md](safety.md).

## Conventions used in this document

```
DIR              A directory path. Drag-and-drop from Finder works on macOS.
SRC, DST         Source and destination paths.
GLOB             A shell glob, e.g. '*.jpg' or 'IMG_*.HEIC'. Quote it.
PATTERN          An argument that's a regex or glob depending on context.
```

Examples use `cardo` as the command name. If you're running the script
directly without installing, substitute `python3 cardo.py` everywhere.

## Common flags

Most destructive commands accept the same set of flags. They're documented
here once instead of repeated under every command.

| Flag | What it does |
| :--- | :--- |
| `-n`, `--dry-run` | Preview without changing anything. Output uses the same format as the real run. |
| `-y`, `--yes` | Skip the time-estimate confirmation prompt. Tiny operations auto-skip the prompt; big ones don't. |
| `-r`, `--recursive` | Descend into subdirectories. Without this, only direct children of the target are considered. |
| `-p GLOB`, `--pattern GLOB` | Filter the file set to those whose name matches `GLOB`. |
| `-i`, `--interactive` | Confirm each file individually. Useful for rename/move on small batches. |
| `--trash` | Send removed files to the OS Trash (recoverable) instead of permanently unlinking. Requires the `send2trash` package. |
| `--include-unsafe` | Bypass the installation-folder protection check. **NOT recommended**; see [safety.md](safety.md). |
| `--no-undo` | Skip writing the undo log for this run. Use when you definitely won't want to reverse it. |
| `--log [PATH]` | Write a human-readable per-action log. With no value, logs to `~/.cardo/logs/`. With a path, writes to that file. |
| `--report` | Write a self-contained HTML report to `~/.cardo/reports/`. Available on read-only commands and `dedupe` / `verify`. |

## Exit codes

Cardo uses meaningful exit codes so it can be wired into shell pipelines
and cron / CI without parsing output:

| Code | Meaning |
| :--- | :--- |
| `0` | Success / clean / nothing to do |
| `1` | Something concerning happened (failures during the run, corruption detected by `verify`, unreadable files, etc.) |
| `2` | Invalid arguments or refusal to start (e.g. `sync` with `src` inside `dst`) |
| `130` | Interrupted with Ctrl+C |

---

# Read-only commands

These never modify the filesystem. Safe to run with abandon.

## `stats`

Size and count breakdown of a folder, grouped by file-type category.

```bash
cardo stats DIR [-r] [-p GLOB] [-y] [--report]
```

| Flag | What it does |
| :--- | :--- |
| `-r`, `--recursive` | Include subfolders |
| `-p GLOB`, `--pattern GLOB` | Filter to matching files |
| `-y`, `--yes` | Skip the time-estimate prompt on huge trees |
| `--report` | Write an HTML report with the breakdown |

The categories are defined in cardo's built-in mapping (`Images`,
`Documents`, `Code`, `Audio`, `Video`, `Archives`, `Installers`, `Data`,
`Other`) and can be customized in the config file. See
[commands.md](commands.md#config) for adding your own categories.

### Examples

```bash
cardo stats ~/Downloads -r
# → tabular breakdown: how many files in each category, total bytes,
#   share of the tree

cardo stats ~/Pictures -r --report
# → same output to terminal plus an HTML report at
#   ~/.cardo/reports/cardo_stats_<timestamp>.html

cardo stats /Volumes/Backup -r -p '*.tif'
# → only TIFF files counted, useful for "how much TIFF do I have"
```

## `tree`

Print a directory tree, optionally with size annotations.

```bash
cardo tree [DIR] [--max-depth N] [-y] [--report]
```

| Flag | What it does |
| :--- | :--- |
| `--max-depth N` | Stop recursing past depth `N` (root is depth 1) |
| `-y`, `--yes` | Skip the time-estimate prompt |
| `--report` | Write an HTML report with the tree |

`DIR` defaults to the current directory if omitted.

### Examples

```bash
cardo tree
# → current directory tree

cardo tree ~/Projects --max-depth 2
# → just the top two levels — quick way to see a project's shape

cardo tree /Volumes/Backup --max-depth 3 --report
# → tree as HTML for sharing or archiving
```

Files include size in parentheses; directories don't.

## `search`

Find files by name, size, or modification age.

```bash
cardo search DIR [-r] [-p GLOB] [--min-size KB] [--max-size KB]
               [--older-than DAYS] [--newer-than DAYS] [--name-regex REGEX]
               [-y] [--report]
```

| Flag | What it does |
| :--- | :--- |
| `--min-size KB` | Only files at least this size (kilobytes) |
| `--max-size KB` | Only files at most this size |
| `--older-than DAYS` | Only files modified more than N days ago |
| `--newer-than DAYS` | Only files modified within the last N days |
| `--name-regex REGEX` | Filter by Python regex against the filename |
| `-r`, `--recursive` | Include subfolders |
| `-p GLOB`, `--pattern GLOB` | Glob filter (combinable with --name-regex) |
| `--report` | HTML report of matches |

Filters are AND-combined: a file must satisfy all of them to be reported.

### Examples

```bash
cardo search ~/Downloads -r --older-than 90 --min-size 100000
# → files >100 MB in Downloads not touched in 90+ days
#   (the classic "what's safe to clean" query)

cardo search ~/Pictures -r --name-regex '^IMG_\d{4}\.HEIC$'
# → strictly-named iPhone imports

cardo search /Volumes/Backup -r --newer-than 7 -p '*.psd'
# → Photoshop files saved in the last week
```

Output is one file per line with size and mtime. With `--report`, you
get an HTML page that's sortable by column.

## `name-clash`

Find files that share a name across the tree — useful for finding
mistakenly-duplicated imports or collision risks before flattening folders.

```bash
cardo name-clash DIR [-p GLOB] [--ignore-ext] [--ignore-case] [-y] [--report]
```

| Flag | What it does |
| :--- | :--- |
| `-p GLOB`, `--pattern GLOB` | Glob filter |
| `--ignore-ext` | Match on stem only (treat `photo.jpg` and `photo.png` as the same name) |
| `--ignore-case` | Treat `Photo.JPG` and `photo.jpg` as the same name |
| `--report` | HTML report of clashes |

This is `dedupe`'s "fast cousin" — it doesn't read file contents, only
filenames. Useful when:

- You're about to flatten subfolders into one folder and want to know
  what'll collide
- You've imported photos multiple times and want to spot the obvious
  duplicates without committing to a hash scan
- You're auditing a project for accidentally-identical resource names

### Examples

```bash
cardo name-clash ~/Photos -r
# → groups of files sharing exact names, with their paths

cardo name-clash ~/Music --ignore-case --ignore-ext
# → 'Track 1.mp3' and 'Track 1.flac' and 'track 1.mp3' all collide

cardo name-clash ~/Downloads -r --report
# → HTML report — easy to scan visually
```

Output groups clashes together. Empty if no clashes exist.

---

# Destructive commands

All of these change the filesystem and run the safety preflights (time
estimate + installation-folder protection). They share the [common flags](
#common-flags) above.

## `copy`

Copy files from one location to another. The simplest destructive command;
the source is left intact.

```bash
cardo copy SRC DST [-r] [-p GLOB] [--overwrite] [common flags]
```

| Flag | What it does |
| :--- | :--- |
| `-r`, `--recursive` | Copy subdirectories too |
| `-p GLOB`, `--pattern GLOB` | Glob filter on filenames |
| `--overwrite` | Replace existing destination files. Default: rename with a numeric suffix (`file.txt`, `file (1).txt`) |

`SRC` may be either a file or a directory. `DST` is created if missing.

### Examples

```bash
cardo copy ~/Documents/report.pdf ~/Backups/
# → single-file copy

cardo copy ~/Photos/2024 /Volumes/External/Photos -r
# → copy the entire 2024 folder; existing files in dst preserved
#   with renamed duplicates

cardo copy ~/Scratch /Volumes/External/Scratch -r --overwrite -n
# → dry-run preview of an overwriting recursive copy

cardo copy ~/Downloads /tmp/quarantine -p '*.dmg' -r
# → copy just the .dmg files out, preserving folder structure
```

Copies are **not undoable** via `cardo undo` — the source is intact, so
there's nothing to reverse. If you want to remove the copies later, use
`cardo dedupe` to find them or `cardo move` if you wanted a move all along.

## `move`

Move files from one location to another. Like `copy` but the source is
deleted after a successful copy.

```bash
cardo move SRC DST [-r] [-p GLOB] [--overwrite] [common flags]
```

The flag set is identical to `copy`. The difference is consequence: this
**is** reversible via `cardo undo` because the move is recorded in the
undo log.

### Examples

```bash
cardo move ~/Downloads/old-project ~/Archive/
# → a single folder, moved

cardo move ~/Pictures/2023 /Volumes/Photos -r
# → moving a year's worth of photos to external storage

cardo move ~/Desktop /tmp/desktop-cleanup -p '*.png' -r --trash
# → "trash" mode for move means: if the move overwrites existing files
#   in dst, the existing ones go to the OS trash instead of being
#   silently replaced
```

### Undo

`cardo undo` after a `move` puts each moved file back at its original
path. Refuses to overwrite a file the user re-created at the original
location (use `--force` to override). See [undo](#undo).

## `rename`

Bulk-rename files using one or more transformation modes.

```bash
cardo rename DIR [--regex PATTERN REPL] [--prefix STR] [--suffix STR]
                 [--lower] [--upper] [--numbered TEMPLATE] [--start N]
                 [--ext EXT] [--overwrite] [-i] [common flags]
```

| Flag | What it does |
| :--- | :--- |
| `--regex PATTERN REPL` | Apply Python regex substitution to the filename stem |
| `--prefix STR` | Prepend a string |
| `--suffix STR` | Append a string (to the stem, before the extension) |
| `--lower` | Lowercase the stem |
| `--upper` | Uppercase the stem |
| `--numbered TEMPLATE` | Replace the stem with a numbered template, e.g. `'photo_{:03d}'` produces `photo_001`, `photo_002`, etc. |
| `--start N` | Starting number for `--numbered` (default `1`) |
| `--ext EXT` | Change the extension. Pass `--ext ''` to strip it entirely. |
| `--overwrite` | Allow renames that would overwrite existing files. Default: skip with a warning. |
| `-i`, `--interactive` | Confirm each rename individually |
| `-r`, `--recursive` | Rename in subfolders too |
| `-p GLOB`, `--pattern GLOB` | Glob filter |

Multiple modes can be combined — they apply in this order: regex → prefix
→ suffix → lower/upper → numbered → ext. The renames are computed first
into a plan, then executed.

### Examples

```bash
cardo rename ~/Photos/Lightroom-export --regex '^IMG_' 'beach-2024-' -r
# → IMG_0001.jpg → beach-2024-0001.jpg across the tree

cardo rename ~/Downloads -p '*.HEIC' --lower
# → all .HEIC files become .heic in name (extension lowercased too)

cardo rename ~/Project/renders --numbered 'frame_{:04d}' --ext jpg
# → renumbered: frame_0001.jpg, frame_0002.jpg, … in directory order

cardo rename ~/Stuff --prefix 'archive_' --suffix '_v1' -n
# → dry-run preview of adding a prefix and suffix to every name

cardo rename ~/Notes --ext md -p '*.txt'
# → .txt files renamed to .md, contents unchanged
```

### Notes

- Renames inside the same directory only — this command doesn't move
  files between folders. For that, use `move`.
- The "stem" is the filename minus its extension. `--prefix` and
  `--suffix` only touch the stem, never the extension; use `--ext` to
  change extensions.
- If two files would end up with the same name, the second is renamed
  with a numeric suffix (or skipped if `--overwrite` is off and the
  target already existed before this run).
- Renames are recorded in the undo log; `cardo undo` reverses them.

### Renames inside install folders

`rename` is protected by the same installation-folder check as the other
destructive commands. Renaming `License.txt` to `LICENCE.txt` inside an
Adobe folder usually breaks the app — cardo will refuse without
`--include-unsafe`. See [safety.md](safety.md).

---

## `organize`

Sort files into category subfolders by file extension. After running, your
`Downloads` (or wherever) is partitioned into `Images/`, `Documents/`,
`Code/`, etc.

```bash
cardo organize DIR [common flags]
```

Operates only on files directly inside `DIR` unless `-r` is set. Files
already in their correct category folder are left alone (no-op).

The default categories:

| Category | Extensions (partial list) |
| :--- | :--- |
| `Images` | `.jpg .jpeg .png .gif .heic .tif .webp .raw .cr2 …` |
| `Documents` | `.pdf .doc .docx .txt .md .rtf .pages …` |
| `Code` | `.py .js .ts .go .rs .c .cpp .java .rb …` |
| `Audio` | `.mp3 .wav .flac .aac .m4a .ogg …` |
| `Video` | `.mp4 .mov .avi .mkv .webm …` |
| `Archives` | `.zip .tar .gz .7z .rar .bz2 .xz …` |
| `Installers` | `.dmg .pkg .msi .exe .deb .rpm .appimage` |
| `Data` | `.csv .json .xml .yaml .toml .sql …` |
| `Other` | everything else |

These categories — and what extensions go where — are customizable via
the config file. See [commands.md](commands.md#organize).

### Examples

```bash
cardo organize ~/Downloads -n
# → preview: shows each move that would happen

cardo organize ~/Downloads -y
# → run it: ~/Downloads now contains Images/, Documents/, etc.

cardo organize ~/Desktop -r --trash
# → also organize subfolders; if any file collides with an existing one
#   in a category folder, the older one goes to trash

cardo organize ~/Photos/2024-import -p '*.heic' -r
# → only HEIC files moved; other types stay where they are
```

### Behavior notes

- **Empty source files are still moved** — they get categorized as
  `Other` (no extension match) or whatever their extension maps to.
- **Collisions get a numeric suffix** — if `Images/photo.jpg` already
  exists, the moved file becomes `Images/photo (1).jpg`. Pass
  `--overwrite` (via `move`'s machinery) to replace instead.
- **The category folders are created on demand.** Empty categories don't
  get a folder.

### Undo

Recorded in the undo log; `cardo undo` puts every moved file back at its
original parent directory. `cardo restore` lets you cherry-pick which
files to put back (e.g. "I organized 500 files, undo just the 5 PDFs").

---

## `dedupe`

Find and optionally remove duplicate files by content hash. The most
powerful — and most dangerous — command in cardo, so it has three modes
with different aggression levels.

```bash
cardo dedupe DIR [--mode {quick,standard,paranoid}] [--min-size KB]
                 [--include-empty] [--no-cache] [--workers N]
                 [--trash] [--report] [common flags]
```

### Modes

| Mode | What it does |
| :--- | :--- |
| `quick` | Metadata-only triage. Groups files by `(size, basename, mtime)`. Reports likely duplicates without hashing anything, without deleting anything. Fast — minutes for huge trees. Read-only. |
| `standard` (default) | Two-pass SHA-256. First pass hashes only files with matching sizes; second pass full-hashes the candidates. Deletes duplicates automatically (or trashes them with `--trash`). |
| `paranoid` | Same as `standard`, plus byte-by-byte verification of every duplicate immediately before deletion. Catches the astronomically rare hash collision and any tampering between hash and delete. Slower. |

### Flags

| Flag | What it does |
| :--- | :--- |
| `--mode MODE` | One of `quick` / `standard` / `paranoid` (default: `standard`) |
| `--min-size KB` | Ignore files smaller than this. Default: 4 KB. Small files clog the count but reclaim ~nothing. |
| `--include-empty` | Include 0-byte files (skipped by default — they all match each other) |
| `--no-cache` | Disable the persistent hash cache. Re-hashes everything from scratch. |
| `--workers N` | Parallel hashing threads. `0` (default) means `min(8, CPU count)`; `1` is serial. |
| `--trash` | Send duplicates to the OS trash instead of unlinking. Requires `send2trash`. |
| `--report` | Write an HTML report listing every duplicate set found |
| `-r`, `--recursive` | Descend into subfolders |
| `-p GLOB`, `--pattern GLOB` | Glob filter |
| `--include-unsafe` | Bypass installation-folder protection (rarely correct) |
| `-n`, `--dry-run` | Hash and report duplicates but don't delete |

### How duplicates are kept vs. deleted

Within a duplicate set, cardo keeps the file with the **shortest path**
and deletes the others. Ties are broken alphabetically. This is the most
predictable rule — if you run dedupe twice in a row, the same files
survive. The "kept" file is shown explicitly in the report.

### Examples

```bash
cardo dedupe ~/Pictures -r --mode quick --report
# → no deletion; HTML report of suspected duplicates by name+size

cardo dedupe ~/Downloads -r --trash -y
# → standard mode, duplicates go to OS trash, no confirmation

cardo dedupe /Volumes/Backup -r --mode paranoid --min-size 1024
# → only files ≥ 1 MB, byte-verified before deletion

cardo dedupe ~/Photos -r --pattern '*.jpg' --workers 16
# → only JPEGs, 16 parallel hash workers (good for SSDs)
```

### The persistent hash cache

Hashes are stored in `~/.cardo/cache/hashes.json` keyed by
`(path, size, mtime) → sha256`. A second `dedupe` run reuses these
hashes for unchanged files, so repeated dedupes are fast. This cache is
also what powers `cardo verify` for bit-rot detection.

To clear the cache: `rm ~/.cardo/cache/hashes.json`. To skip it for a
single run: `--no-cache`.

### Safety notes

- The installation-folder protection check fires for `dedupe` too —
  duplicates inside `.app` bundles are protected by default.
- The hash cache uses size and mtime as the freshness key. If a file's
  mtime is unchanged but its contents differ, dedupe will use the
  cached hash and may miss the change. This is the case `verify` exists
  to catch.
- Hash collisions for SHA-256 are theoretically possible but you will
  almost certainly never see one. `paranoid` mode exists in case you
  want to be sure regardless.

---

## `clean`

Remove empty subdirectories from a tree, bottom-up. Useful after a heavy
`organize` or `move` that left empty parent folders behind.

```bash
cardo clean DIR [-n] [-y] [--trash] [--log] [--no-undo]
                [--include-unsafe]
```

| Flag | What it does |
| :--- | :--- |
| `-n`, `--dry-run` | Preview without removing anything |
| `-y`, `--yes` | Skip the time-estimate prompt |
| `--trash` | Send empty directories to OS trash instead of `rmdir`'ing them |
| `--include-unsafe` | Bypass installation-folder protection |
| `--no-undo` | Skip writing the undo log |
| `--log [PATH]` | Per-action log |

Walks the tree bottom-up, so removing `a/b/c/` (empty) makes `a/b/` empty,
which then gets removed too. The user-passed root is never removed —
only its children.

### Examples

```bash
cardo clean ~/Downloads -n
# → preview every empty directory that would be removed

cardo clean ~/Downloads -y --trash
# → run it; empty dirs go to trash

cardo clean ~/Projects/old-import -r
# (note: clean is implicitly recursive; -r has no effect)
```

### Important safety story

Cardo's installation-folder protection was added because an early version
of `clean` was pointed at the root of a working SSD and started trashing
the (legitimately empty) preset folders inside Adobe InDesign, Cinema
4D, and Motion.app. The current `clean` detects this pattern and refuses
to touch detected install folders without confirmation. See
[safety.md](safety.md) for the full story.

If you're cleaning a working drive that happens to contain installed
applications, the protection check is doing exactly what you want it to
do. Read the summary it prints and only set `--include-unsafe` if you
fully understand what'll be removed.

### Undo

The `rmdir` path is recorded in the undo log: `cardo undo` recreates the
empty directories. The `--trash` path is NOT recorded — recovery in that
case is via the OS trash, which already provides the same affordance.

---

## `sync`

One-way mirror from a source folder to a destination folder. Like
rsync, but with cardo's safety preflights and consistent UX.

```bash
cardo sync SRC DST [-c] [--mirror] [--trash] [--follow-symlinks]
                   [--no-cache] [-p GLOB] [common flags]
```

| Flag | What it does |
| :--- | :--- |
| `-c`, `--checksum` | Compare files by SHA-256 instead of mtime+size. Definitive but reads every byte. |
| `--mirror` | Also delete files in destination that aren't in source. **Off by default** — sync is additive unless you opt in. |
| `--trash` | When `--mirror` deletes extras, route them through OS trash |
| `--follow-symlinks` | Dereference source-side symlinks. Default: copy them as symlinks. |
| `--no-cache` | Disable the hash cache (only relevant with `--checksum`) |
| `-p GLOB`, `--pattern GLOB` | Glob filter |
| `-n`, `--dry-run` | Preview the full plan |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--include-unsafe` | Bypass installation-folder protection |
| `--log [PATH]` | Per-action log |

### How "changed" is decided

By default, a file is updated if `src` is newer **and** of different size,
or if it doesn't exist in `dst`. A 2-second mtime tolerance absorbs
filesystem precision loss.

With `--checksum`, files are compared by SHA-256. Slower but defeats
"touched but unchanged" false positives — useful when you've copied a tree
between filesystems that normalized mtimes.

### Files newer in destination

If `dst/foo` is **newer** than `src/foo`, cardo leaves `dst/foo` alone.
This is deliberate — sync should never silently destroy work that's
newer on the destination. If you want to overwrite regardless, use `move`
or delete the destination file first.

### Refusals

`sync` refuses to start if:

- `src` doesn't exist or isn't a directory
- `src` and `dst` are the same path
- `dst` is inside `src` (would recurse)
- `src` is inside `dst` (would source from inside its own target)
- Either path is inside an installed-application folder (protection
  check; override with `--include-unsafe`)

### Examples

```bash
cardo sync ~/Documents /Volumes/Backup/Documents -n
# → preview: what would be copied / updated, in what order

cardo sync ~/Documents /Volumes/Backup/Documents -y
# → additive mirror — new and changed files copied,
#   nothing in the destination is deleted

cardo sync ~/Documents /Volumes/Backup/Documents --mirror --trash -y
# → also delete anything in Backup/Documents that's not in ~/Documents,
#   but route the deletions through OS trash

cardo sync ~/PhotosWorking /Volumes/Archive/Photos --checksum
# → byte-for-byte comparison; catches files that lost mtime precision

cardo sync ~/src/cardo /Volumes/Backup/projects/cardo -p '*.py'
# → only sync .py files, leave everything else alone
```

### Not fully undoable

Sync is **not** registered as an undoable command. Reasons:

- New files copied to dst → reversible (just delete them), but
- Updated files in dst overwrote old content → not reversible (we
  don't store the old content)
- With `--mirror`, deleted extras → not reversible unless trashed

The honest answer: routine sync runs go through `--trash` if you care
about reversibility of the deletion side. There's no special undo path
for sync.

---

# Recovery and verification commands

## `verify`

Re-hash files in a tree and compare against the persistent hash cache.
Read-only. Distinguishes **silent corruption** (content changed, metadata
unchanged — bit-rot, disk error, tampering) from normal edits.

```bash
cardo verify DIR [-r] [-p GLOB] [--workers N] [--no-add-new]
                 [--show-modified] [--show-missing] [-y] [--report]
```

| Flag | What it does |
| :--- | :--- |
| `-r`, `--recursive` | Descend into subfolders |
| `-p GLOB`, `--pattern GLOB` | Glob filter |
| `--workers N` | Parallel hashing threads (`0` = auto: `min(8, CPU)`; `1` = serial) |
| `--no-add-new` | Don't add untracked files to the cache. Default: add them so the next verify run can check them. |
| `--show-modified` | List the paths of modified files (default: just count them) |
| `--show-missing` | List the paths of missing/orphaned files (default: count only) |
| `-y`, `--yes` | Skip the time-estimate prompt |
| `--report` | Write an HTML report colored by classification |

### Classifications

Each file gets one of these statuses:

| Status | Meaning |
| :--- | :--- |
| **Unchanged** | Hash matches the cached value. Everything is fine. |
| **New** | Not in cache. Either freshly created or this is your first verify run on this tree. Added to the cache by default. |
| **Modified** | Hash differs *and* size or mtime changed. Normal edit. |
| **Corrupted** | Hash differs but size and mtime are unchanged. **This is the alarming case** — disk corruption, bit-rot, or tampering. |
| **Unreadable** | Could not hash (permission denied, I/O error). |
| **Missing** | Cache entry exists but the file is gone from disk. (Reported as "orphan".) |

### Establishing a baseline

The first time you run `verify` on a tree, every file is reported as
"new" and added to the cache. The second run is the actual check.

If you've ever run `dedupe` on a tree, those files are already in the
cache and `verify` will check against the hashes recorded then.

### Examples

```bash
cardo verify /Volumes/Photo-Archive -r -y
# → first run on the archive: builds the baseline, exits 0

# (some time passes; you suspect a disk might be aging)

cardo verify /Volumes/Photo-Archive -r -y --show-modified
# → second run: hash-checks everything against the baseline
#   non-zero exit code if any corruption found

cardo verify ~/Important -r --workers 1 --no-add-new
# → strict mode: only check what's already cached; don't expand the
#   baseline; one thread (lower system load)

cardo verify /Volumes/Archive -r --report
# → HTML report colored by status: red for corrupted, yellow for
#   modified, orange for missing
```

### Exit code

| Code | When |
| :--- | :--- |
| `0` | Clean — no corruption, no unreadable files. Modified and new are not errors. |
| `1` | At least one file is corrupted **or** unreadable. |

This makes `verify` suitable for cron / scheduled tasks:

```bash
# Weekly check on the archive; email me if corruption is detected
0 3 * * 0  cardo verify /Volumes/Archive -r -y \
              || mail -s "Archive verify failed" me@example.com
```

### Mtime tolerance

Many filesystems and tools (FAT32, HFS+, `touch -d`, rsync without
`--modify-window`) normalize mtime to whole seconds. Strict comparison
would mis-classify a file restored from such a source as "corrupted" —
size and mtime match the cache to the second, but the recorded mtime
was sub-second. To absorb this, `verify` allows a **2-second mtime
tolerance**. Tight enough to still distinguish corruption from genuine
edits.

---

## `undo`

Reverse the most recent reversible run (move, rename, organize, clean)
all at once.

```bash
cardo undo [--list] [-n] [-y] [--force] [--log [PATH]]
```

| Flag | What it does |
| :--- | :--- |
| `--list` | Show recent undo logs without doing anything |
| `-n`, `--dry-run` | Print what would be reversed; change nothing |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--force` | Overwrite destinations that already exist when reversing |
| `--log [PATH]` | Write a human-readable log of the undo run |

### How undo finds the target run

`undo` operates on the most recent run that:
1. Has any reversible actions in it (move / rename / organize / clean),
2. Hasn't been fully undone already, and
3. Has at least one entry not yet consumed by `restore`.

If you've used `restore` to cherry-pick entries from the most recent run,
`undo` will only reverse the remaining ones (and reports this in its
preflight summary):

```
  Will undo this run:
    command:    cardo organize
    argv:       organize /Users/me/Downloads -r -y
    completed:  2026-05-14 21:01:58
    actions:    197 pending (3 already reversed via `restore`)
```

### Reverse operations

| Original op | Inverse |
| :--- | :--- |
| `move src → dst` | Move `dst` back to `src` |
| `rename old → new` | Rename `new` back to `old` |
| `organize file → Cat/file` | Move back to original parent |
| `clean rmdir <path>` | Recreate `<path>` as empty directory |
| `dedupe` (any) | **Not undoable** — files were deleted/trashed |
| `sync` (any) | **Not undoable** — see [sync](#sync) |
| `copy` (any) | **Not undoable** — source is intact, nothing to reverse |

### Examples

```bash
cardo undo --list
# → recent runs, newest first, with availability status

cardo undo -n
# → preview what would be reversed by 'cardo undo'

cardo undo -y
# → run it; user-confirmed by virtue of -y

cardo undo --force
# → overwrite any destination files re-created by the user since the
#   original run
```

### When undo can fail

- **Destination exists** — someone re-created a file at the original
  path. Default: refuse, report the path. With `--force`: overwrite.
- **Source missing** — the moved/renamed file is no longer at its
  current location (deleted, moved elsewhere, on an unmounted drive).
  Reported in the failure breakdown.
- **Permission denied** — usual filesystem reason. Reported.

A partial undo (some entries reversed, some failed) marks the entries
that *did* succeed as consumed in the undo log. Running `undo` again on
the same log will retry only the failures.

---

## `restore`

Selectively reverse individual entries from any past run's undo log.
Whereas `undo` is all-or-nothing on the most recent run, `restore` lets
you pick which entries to roll back from any run in history.

```bash
cardo restore [LOG] [--list] [--range SPEC] [--grep PATTERN]
              [-n] [-y] [--force] [--log [PATH]]
```

| Flag | What it does |
| :--- | :--- |
| `LOG` | Undo log filename (in `~/.cardo/undo/`) or full path. Omit to use the most recent pending log. |
| `--list` | Show recent undo logs (same as `undo --list`) |
| `--range SPEC` | Select entries by range syntax, e.g. `'1-5, 8, 11-15'`. Skips the interactive picker. |
| `--grep PATTERN` | Select entries whose source or destination matches a glob. Skips the picker. |
| `-n`, `--dry-run` | Show what would be reversed |
| `-y`, `--yes` | Skip the confirmation prompt |
| `--force` | Overwrite destinations that already exist |

### Interactive picker

When you run `cardo restore` without `--range` or `--grep`, you get a
numbered list of entries and a prompt:

```
  Entries (✓ = already reversed by an earlier restore):
       1.  move    /Users/me/Downloads/Images/photo1.jpg ← /Users/me/Downloads/photo1.jpg
       2.  move    /Users/me/Downloads/Images/photo2.jpg ← /Users/me/Downloads/photo2.jpg
   ✓   3.  move    /Users/me/Downloads/Images/photo3.jpg ← /Users/me/Downloads/photo3.jpg
       4.  move    /Users/me/Downloads/Documents/doc1.pdf ← /Users/me/Downloads/doc1.pdf
       5.  move    /Users/me/Downloads/Documents/doc2.pdf ← /Users/me/Downloads/doc2.pdf

  Pick entries to reverse. Examples:
    1-5, 8, 11-15      ranges and individual numbers
    a                  all pending entries
    q (or blank)       cancel
  Selection:
```

### Examples

```bash
cardo restore --list
# → same as cardo undo --list

cardo restore --range "2-3"
# → from the most recent pending log, reverse entries 2 and 3
#   (non-interactive)

cardo restore --grep "*.jpg"
# → from the most recent pending log, reverse every entry where
#   the source or destination path matches *.jpg

cardo restore 2026-05-14_21-01-58_organize.jsonl
# → operate on a specific log file, not the most recent

cardo restore 2026-05-14_21-01-58_organize.jsonl --range "5-10" -y
# → reverse entries 5-10 of that specific run, no confirmation
```

### Partial consumption

`restore` updates the log header to track which entries it consumed. The
next `undo --list` shows the status:

```
   1. ~ partial (2/5 entries done)     5 action(s)  2026-05-14 21:01:58  organize
```

When all entries in a log have been consumed (by any combination of
`undo` and `restore`), the full `undone` flag flips and the log shows
`✓ undone` in subsequent listings.

### Force semantics

Same as `undo --force`: refuse to overwrite destinations the user has
re-created since the original run, unless `--force` is set.

---

# Configuration command

## `config`

Show, locate, or initialize the config file at `~/.cardo/config.toml`.

```bash
cardo config show
cardo config path
cardo config init [--force]
```

| Subcommand | What it does |
| :--- | :--- |
| `show` | Print every setting along with where it came from (config file vs. built-in default) |
| `path` | Print the absolute path of the config file |
| `init` | Write a commented starter file. Refuses if one already exists; pass `--force` to overwrite. |

### Examples

```bash
cardo config path
# → /Users/me/.cardo/config.toml

cardo config init
# → creates ~/.cardo/config.toml with comments explaining every setting

cardo config show
# → "trash: true (config: ~/.cardo/config.toml)"
#   "assume_yes: false (built-in default)"
#   ...

# Then edit the file in your favorite editor:
$EDITOR "$(cardo config path)"
```

### Settings reference

See [commands.md](commands.md#config) for the full reference of every settable
option and how the precedence works (CLI flag > config file > built-in
default).

---

# Subcommand index

Alphabetical, for quick reference:

- [`clean`](#clean) — remove empty subdirectories
- [`config`](#config) — show/locate/initialize the config file
- [`copy`](#copy) — copy files to another location
- [`dedupe`](#dedupe) — find and remove duplicate files
- [`move`](#move) — move files to another location (reversible)
- [`name-clash`](#name-clash) — find files with identical names
- [`organize`](#organize) — sort files into category subfolders
- [`rename`](#rename) — bulk-rename files
- [`restore`](#restore) — selectively reverse entries from a past run
- [`search`](#search) — find files by name, size, or age
- [`stats`](#stats) — size/count breakdown of a folder
- [`sync`](#sync) — one-way mirror src → dst
- [`tree`](#tree) — print a directory tree
- [`undo`](#undo) — reverse the most recent reversible run
- [`verify`](#verify) — re-hash files and compare against the cache

See also: [safety.md](safety.md), [design-notes.md](design-notes.md).
