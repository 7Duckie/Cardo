# Safety

Cardo's safety layer is the most opinionated thing about the project.
Every destructive command runs through the same set of checks before
touching the filesystem, and the checks were designed in response to
real damage. This document explains what each check does, when it
fires, how to override it (and when overriding is actually appropriate),
and the philosophy behind the design.

## Philosophy

A file manager has the same problem a surgical knife has: it does
exactly what you tell it to. If what you told it was wrong, it does the
wrong thing efficiently. The standard answer in CLI tools is "type
carefully and read the man page first." That answer is fine for `rm`
because `rm`'s scope is narrow — but cardo runs over trees of thousands
of files at once, and "type carefully" stops scaling around 10,000
actions per command.

So cardo's design takes a different approach: assume the user will
sometimes be wrong, and make wrong instructions visible and reversible
*before* they execute. Concretely:

1. **Tell the user what will happen before it happens.** Every
   destructive command builds a complete plan in memory first. Then it
   shows you the plan — counts, sizes, an estimated duration — and asks
   for confirmation. The plan-then-execute pattern means an "oh wait
   I'm in the wrong directory" realization happens before disk gets
   touched.
2. **Detect categories of damage that aren't obvious.** Some destructive
   actions look fine in isolation but cause cascading damage (e.g.
   removing an "empty" directory that turns out to be inside an Adobe
   product folder). Cardo recognizes these patterns and protects them
   even when the user didn't ask.
3. **Make reversal a normal part of the workflow.** Move, rename,
   organize, and clean all write structured undo logs. `cardo undo`
   reverses the most recent run; `cardo restore` lets you pick which
   entries to roll back. Together they make most accidents recoverable
   without needing backups.
4. **Prefer trash over unlinking.** Optional `--trash` flag on every
   destructive command routes removed files to the OS trash. Operating
   systems already have good trash UI; cardo defers to it rather than
   inventing its own recovery mechanism.

Each of these is opt-out, not opt-in. The protection is on by default;
you have to actively pass `--include-unsafe` to disable it. The trash is
off by default only because we don't want to require `send2trash` for
cardo to work at all — most users should turn it on in their
[commands.md](commands.md#config).

---

## The incident that motivated the protection layer

An early version of cardo (called `filemgr` at the time) was pointed at
the root of a working SSD with the command:

```
filemgr clean /Volumes/Live\ SSD\ -\ NOT\ for\ storing\ files. -y --trash
```

`clean` removes empty subdirectories. It walked the tree bottom-up and
started trashing the (legitimately empty) preset folders inside
installed applications:

```
Trashing: …/Adobe InDesign 2026/Presets/WhatsNewOnboarding/en_GB
Trashing: …/Adobe InDesign 2026/Presets/WhatsNewOnboarding/FlexLayout
Trashing: …/Adobe InDesign 2026/Fonts
Trashing: …/Motion.app/Contents/PlugIns/Compressor/CompressorKit.bundle/…
Trashing: …/MagicQ/MagicQ.app/Contents/Resources/user/show/heads
Trashing: …/Maxon Cinema 4D 2024/resource/modules/asset.module/…
…
```

The files were technically recoverable (they were going to the OS
trash, not being unlinked) and the user caught it before it had run too
far. But it would have broken InDesign, Motion, MagicQ, and Cinema 4D
once those apps tried to load files from folders that no longer existed.

The root cause was structural: dedupe had detailed protection for
installed-application content, but `clean`, `organize`, `move`, and
`rename` didn't consult any of it. They just operated on whatever they
were pointed at. Cardo's current protection layer is the result of
lifting that detection out of dedupe's exclusive use and making every
destructive command call it before doing anything.

If you've ever wondered why cardo has so many preflight checks — that's
why. The cost of one false-positive "Proceed?" prompt is small. The
cost of trashing the guts of an installed application is large enough
to justify the prompt for the next twenty years.

---

## The time-estimate preflight

Before any destructive command executes, cardo:

1. Builds the complete action plan in memory
2. Estimates how long it will take based on file count and total bytes
3. Prints a summary of the plan
4. Asks for confirmation (unless `-y` or `assume_yes = true`)

```
  Plan: 1,247 file(s), 8.3 GB total
  Estimated time for dedupe (hash): ~1m 12s
  Proceed? [y/N]
```

For tiny operations (a handful of files, well under a second), the
prompt auto-skips — the asymmetry between annoyance and danger is the
wrong way around. The threshold is currently set such that the prompt
appears when there's enough work to make a mistake worth noticing.

### Why this matters more than it sounds

The time-estimate prompt does two jobs:

- **Sanity check on scale.** "I'm about to organize 12,000 files" is
  qualitatively different from "I'm about to organize 12 files," and
  the difference should be visible to the user.
- **Sanity check on target.** The plan summary includes the user-passed
  path. Reading "Plan: 47,000 file(s) under `/Volumes/Live SSD`" when
  you meant `~/Downloads` is the last moment to catch the mistake.

### Bypassing the prompt

- `-y` on the command line skips it for that run only
- `assume_yes = true` in `[defaults]` of `~/.cardo/config.toml` skips
  it globally — with a one-line reminder in the output so it's not
  silently absent

The prompt is not a security feature; it's a UI affordance. Skipping it
for known-good workflows (scripted backups, CI pipelines) is fine.
Skipping it because you find it annoying on interactive use is mostly
fine but worth a moment of self-reflection: cardo's first version
didn't have it, and that's how the install-folder incident happened.

---

## The installation-folder protection

This is the most important check in the toolkit. It runs on every
destructive command — `move`, `rename`, `organize`, `clean`, `dedupe`,
`sync` — and refuses to touch files that look like they belong to an
installed application without an explicit confirmation.

### What gets detected

Cardo recognizes installed applications by several heuristics:

#### Path-segment suffixes

Any path containing a segment ending in one of these is treated as
inside a managed package:

| Suffix | What it is |
| :--- | :--- |
| `.app` | macOS application bundle |
| `.framework` | macOS shared framework |
| `.bundle` | macOS plug-in bundle |
| `.kext` | macOS kernel extension |
| `.plugin` | various plug-in bundles |
| `.xpc` | macOS XPC service bundle |
| `.lproj` | macOS localization folder |
| `.lrcat-data`, `.lrdata`, `.lrlibrary` | Adobe Lightroom |
| `.photoslibrary` | macOS Photos library |

Files inside *any* of these are protected, regardless of where the
suffix appears in the path. This is the broadest and cheapest check.

#### Install-folder detection

A folder is treated as an installation root if it has at least two of
these signals:

- **App-style subfolders** (`Plug-Ins`, `Resources`, `Contents`,
  `Frameworks`, `MacOS`, `lib`, `bin`, `share`)
- **Vendor files** present at the top level (`License.txt`,
  `EULA.txt`, `version.txt`, `LICENSES`, etc.)
- **Reverse-DNS-style child folders** (`com.adobe.foo`,
  `net.maxon.bar`, `org.jetbrains.baz`)
- **Versioned product name** (the folder is named like
  `Adobe Photoshop 2026`, `Cinema 4D 2024`, `IntelliJ IDEA Ultimate
  2024.3`)
- **Binary-heavy contents** at the top level (`.dylib`, `.so`, `.dll`,
  many executables)

When two or more signals are present, cardo treats the folder as an
installation root. Everything inside it is then protected.

#### Specific filenames

Some filenames are always-protected because removing them tends to
break things even outside install folders:

- `.DS_Store`, `Thumbs.db`, `desktop.ini` — system metadata
- `LOCK`, `flock` — exclusive-access markers used by databases and
  cache stores

### What "protection" actually does

When the protection check fires, cardo:

1. Builds the complete plan as usual
2. Walks the plan and classifies each action as **safe** or
   **protected**
3. If any actions are protected, prints a categorized summary:

```
  ⚠ Protection: skipping 8 action(s) that would touch installed-application
    content:
         4× inside installation folder 'Adobe InDesign 2026'
         2× inside installation folder 'Maxon Cinema 4D 2024'
         2× inside .app package

    First 5 of 8:
      • /Volumes/SSD/Adobe InDesign 2026/Plug-Ins
      • /Volumes/SSD/Adobe InDesign 2026/Presets
      • …

  clean will proceed with 1,239 safe action(s) and skip the 8 protected one(s).
  Proceed? [y/N]
```

4. Asks once for confirmation
5. If you say yes, runs the safe portion of the plan and reports the
   skipped protected actions under "Skipped" in the summary
6. If you say no, aborts the entire run — including the safe actions —
   so a wrong target doesn't get half-processed

The asymmetry matters: protected actions are skipped by default, and you
have to actively confirm to do *anything*. This means if you point cardo
at the wrong drive and 99% of the work is protected, you'll see that
and bail before any of the remaining 1% runs.

### Pointing cardo *at* an install folder

A separate case from "operating on a tree that contains install folders"
is "operating on a tree that **is** an install folder." If you run
`cardo clean /Applications/Adobe\ InDesign\ 2026`, cardo recognizes that
the path itself looks like an installation and prints an extra warning
before the protection prompt:

```
  ⚠ the folder you specified (/Applications/Adobe InDesign 2026) itself
    looks like an installed application:
  ⚠   — contains app-style subfolders: Plug-Ins, Resources, Contents
  ⚠   — vendor files present: License.txt, EULA.txt
  ⚠   — folder name 'Adobe InDesign 2026' looks like a versioned product
```

Then the normal protection summary follows. This is meant for the case
where you typed the wrong path; if you really did intend to operate on
that folder, read the warning carefully and decide before answering.

---

## Overriding the protection: `--include-unsafe`

When you genuinely need to operate inside an installed application —
the case is rare but does exist — pass `--include-unsafe`:

```bash
cardo dedupe /Applications/SomeApp.app -r --include-unsafe
```

This disables the protection check entirely for that single run. Cardo
prints a one-line acknowledgement so it's visible:

```
  --include-unsafe: skipping protection check for managed packages.
```

### When this is actually appropriate

- You're a developer working *on* an `.app` you built and want to dedupe
  its bundled resources
- You're cleaning up files *you* placed inside a managed folder (e.g.
  your own preset exports inside a Cinema 4D scripts directory)
- You're operating on a tree of *uninstalled* applications (e.g. a
  backup of app bundles that the OS doesn't actively reference)
- You're using cardo as part of a build pipeline that intentionally
  manipulates package internals

### When it's not appropriate

- You don't want to read the protection summary
- You're operating on a working system's `/Applications`
- You don't actually understand what's about to be touched

The flag is intentionally a per-command argument, not a config setting.
You can't enable it globally; you have to decide every time. See
[commands.md](commands.md#config) and [safety.md](safety.md)
for the reasoning.

### Refining the heuristics

The detection is conservative but not perfect. Two failure modes worth
knowing about:

- **False positives** — a folder that isn't really an installed app
  trips the heuristics (e.g. a project folder with `License.txt`, a
  `Resources/` subfolder, and a `.lproj` for translation). Use
  `--include-unsafe` for that specific run, and consider opening an
  issue with the folder's contents so the heuristic can be refined.
- **False negatives** — a real install folder doesn't trip the
  heuristics, and cardo modifies it. Less common (we err on the side of
  protection) but possible. If you encounter one, open an issue with
  the folder structure; this is the highest-priority kind of bug
  report.

---

## Trash mode: `--trash`

Every destructive command that removes files supports a `--trash` flag.
With it, removed files go to the OS Trash instead of being unlinked
permanently:

| OS | Where things go |
| :--- | :--- |
| macOS | The user's Trash, visible in Finder |
| Linux (most desktops) | `~/.local/share/Trash/` per the freedesktop spec |
| Windows | The Recycle Bin |

### Requirements

Trash support comes from the `send2trash` Python package. Install with:

```bash
pip install send2trash
```

If it's not installed, `--trash` is rejected with a clear message rather
than silently falling back to permanent deletion. The behavior is
"fail-loud" — we don't want a user who relies on `--trash` for safety to
have it silently disabled because of a dependency hiccup.

### When to use it

The honest answer: almost always, unless you have a specific reason
not to. Trash adds a tiny amount of overhead (a metadata write on most
filesystems) and gives you a recovery affordance for the most common
mistake category — "I deleted that and now I want it back."

The most common reasons *not* to use it:

- **No space on the trash volume.** On Linux, the trash for a given
  filesystem lives on that filesystem. Moving a 200 GB file to trash
  on a full disk fails. Without `--trash`, the file is unlinked
  immediately.
- **Headless servers.** No interactive trash to recover from. The OS
  trash semantics still work, but the user-facing affordance is missing.
- **Cron / batch runs.** If you're sure the action is correct (e.g.
  deduping a backup directory against a known-good source), `--trash`
  is just clutter accumulating in the trash folder.

### Setting it globally

If you want every destructive command to default to trash mode, add this
to `~/.cardo/config.toml`:

```toml
[defaults]
trash = true
```

CLI flags still win, so you can pass `--no-trash` on a specific run.
Actually — `--no-trash` doesn't exist; to opt out of a config-on
default, you'd need to add it. As of 1.0, the config setting is "on or
off" and individual commands inherit it. Either run without the config
override, or remove the config setting.

### Trashed files and undo

Files trashed by `dedupe --trash` or `clean --trash` are recoverable
**only via the OS trash**, not via `cardo undo`. The undo log only
records reversible operations (move, rename, rmdir without trash);
deletions are not recorded because cardo can't reverse them without
storing the deleted content.

This is by design — the OS trash already provides recovery for this
case, and duplicating that machinery in cardo would be both effort and
confusion. If you might want to recover something, run with `--trash`
and use Finder / Files / your trash app.

---

## Dry-run: `-n` / `--dry-run`

Every destructive command supports a dry-run mode that shows exactly
what would happen without changing anything:

```bash
cardo organize ~/Downloads -r -n
cardo clean /Volumes/Backup -n
cardo dedupe ~/Photos -r --mode standard -n
```

Dry-run uses the **same output format** as the real run, so reading a
dry-run output is the same skill as reading a real one. Where the real
run says `Moving: a → b`, dry-run says `Would move: a → b`. The plan
counts and time estimates are computed identically.

### Dry-run interacts with the protection prompt

In dry-run mode, the protection check still runs and still prints its
summary, but the "Proceed?" prompt is auto-accepted — there's no harm in
proceeding because nothing will change anyway. You see exactly what the
real run would show.

### When to use dry-run

- **Before any first run of a command you haven't used before** — it's
  free and shows you what cardo's plan looks like
- **When the target is large or unfamiliar** — `cardo organize ~/Volumes/External -r -n`
  before committing
- **In scripts**, before the real command, to capture the plan as a log
  for audit

### Limits of dry-run

Dry-run is not a perfect simulation. Things that don't happen in dry-run
that would happen in a real run:

- The plan-then-execute split — a real run could fail an action between
  building the plan and executing it (e.g. permission denied on a
  specific file). Dry-run can't predict per-file failures.
- Hashing in `dedupe` — dry-run dedupe still hashes (it has to, to
  identify duplicates), so the "no I/O" promise is partial.
- Disk-full conditions — dry-run doesn't write, so it can't detect
  that a real copy would run out of space.

These edge cases aside, dry-run output is normally identical to real-run
output minus the side effects. Trust it.

---

## Undo and restore as recovery

`cardo undo` and `cardo restore` together provide a structured recovery
path for the destructive operations that support them: `move`, `rename`,
`organize`, and `clean` (rmdir branch).

See [commands.md#undo](commands.md#undo) and
[commands.md#restore](commands.md#restore) for the full reference.
Safety-relevant points:

### What's reversible

| Operation | Reversible by undo/restore? |
| :--- | :--- |
| `move src → dst` | Yes — moves `dst` back to `src` |
| `rename old → new` | Yes — renames `new` back to `old` |
| `organize file → Cat/file` | Yes — moves back to original parent |
| `clean rmdir <path>` | Yes — recreates the empty directory |
| `clean trashed <path>` | No (recover via OS trash) |
| `dedupe` deletions | No (recover via OS trash if `--trash` was set) |
| `sync` overwrites | No (overwritten content is gone) |
| `sync` deletions | No (recover via OS trash if `--trash` was set) |
| `copy` | N/A (source is intact) |

The rule: if cardo can reconstruct the inverse from log data alone, it
does. If it would need to store original content somewhere, it doesn't —
that's the OS trash's job.

### What protects undo from itself

A few rules keep `undo` and `restore` from causing damage:

- **Destination-exists check.** If you re-created a file at the
  original path, undo refuses to overwrite it. Pass `--force` if you
  really do want to clobber.
- **Per-entry failure isolation.** If undo can't reverse one specific
  entry (file moved elsewhere, permissions changed, etc.), the rest
  still run. The summary reports failures clearly.
- **Partial consumption tracking.** When `restore` reverses some
  entries from a log, those entries are marked as consumed. A
  subsequent `undo` only reverses what's still pending.
- **No undo of undo.** `undo` itself isn't undoable. If you undo and
  then change your mind, you have to re-run the original command. We
  considered chained undo and decided the confusion would outweigh the
  utility.

### Where the undo log lives

`~/.cardo/undo/{timestamp}_{command}.jsonl`. One file per reversible
run. The format is JSONL — a JSON object per line — with a meta header
on the first line and one entry per action thereafter:

```jsonl
{"_meta": true, "command": "organize", "argv": [...], "started": "...", "completed": "...", "undone": false}
{"op": "move", "from": "/Users/me/Downloads/photo.jpg", "to": "/Users/me/Downloads/Images/photo.jpg", "ts": 1715725863.12}
{"op": "move", "from": "/Users/me/Downloads/doc.pdf", "to": "/Users/me/Downloads/Documents/doc.pdf", "ts": 1715725863.45}
…
```

You can read these files directly with `jq`, `grep`, or any JSON tool.
They're not meant to be primary user-facing output but they're not
hidden either — being able to read them is part of cardo's "no
mysterious state" promise.

### Suppressing the undo log

The `--no-undo` flag on any reversible command skips writing the log
for that run. Use this when:

- You're certain the operation is correct and don't want disk space
  going to undo files (rare; the files are small)
- You're running cardo in a context where undo wouldn't make sense
  (e.g. inside a CI pipeline that resets the filesystem afterward)

There's no config setting to disable undo globally — same reasoning as
`--include-unsafe`. Active choice every time.

---

## End-to-end recovery patterns

A few worked examples of what to do when things go wrong.

### "I just ran organize on the wrong directory."

```bash
cardo undo -n        # see what would be reversed
cardo undo -y        # do it
```

That's it. Unless someone has re-created files at the original paths
since the original run, this puts everything back. If they have, undo
will fail those specific entries and tell you which paths conflict.
Either move the new files aside or pass `--force` to overwrite.

### "I just organized 500 files and want to put 5 of them back."

```bash
cardo restore        # interactive picker — pick the 5
# or
cardo restore --range "12-16"
# or
cardo restore --grep "*.pdf"
```

The remaining 495 stay where organize put them. Subsequent
`cardo undo --list` shows the run as `~ partial`.

### "I dedupe'd a folder and now I think some deletions were wrong."

If you used `--trash`: open your OS trash (Finder Trash, Recycle Bin,
etc.) and restore from there. The trashed files retain their original
paths in most OS trash implementations.

If you didn't use `--trash`: the files are gone. Restore from backup.
This is why `--trash` is recommended for routine use.

### "I clean'd a directory and removed empty dirs I needed."

If you used `--trash`: same as above — recover from OS trash. Empty
dirs go into the trash as empty dirs and are recoverable.

If you didn't use `--trash`:

```bash
cardo undo -y
```

works, because the rmdir branch of `clean` *is* logged to undo. Cardo
will recreate the empty directories. Their (formerly) empty state is
preserved — they're created with no contents because that's what was
recorded.

### "I think a file on my backup drive has bit-rotted."

```bash
cardo verify /Volumes/Backup -r -y
```

If the cache has an entry for the file and the hash now differs:

- **Different + same metadata** → reported as **CORRUPTED**, exit code 1
- **Different + changed metadata** → reported as **Modified**, exit
  code 0

Investigate corruptions before assuming they're benign. A file that
hashes differently with unchanged size and mtime is the textbook sign
of disk-level corruption.

### "I ran sync and want to put a file back that was overwritten."

You can't, via cardo. Sync overwrites don't go through undo. Options:

- Restore from another backup
- Restore from version control if the file was tracked
- Restore from a filesystem snapshot if you have APFS/ZFS/Btrfs

This is the strongest case for using `--trash` with `--mirror`:
extras-in-destination at least end up in the trash. There's no
equivalent for overwrites; sync is one-way and content updates are
final.

---

## What cardo deliberately doesn't do

A few common "safety" patterns cardo intentionally doesn't implement:

- **No automatic backup before destructive operations.** Some tools
  copy the entire affected tree to a `.bak/` folder before running.
  Cardo doesn't — it's expensive, often impossible (no space), and
  would create its own destructive failure modes ("the backup folder
  filled up and the operation got mid-way through").

- **No "are you sure?" prompts beyond the time-estimate one.** Adding
  more prompts dilutes the ones that matter. Users learn to mash
  Enter, and important warnings get blown past. One serious prompt
  per destructive run, with substantive information in it.

- **No interactive recovery UI.** Cardo's undo/restore are CLI
  commands; cardo doesn't pop up a window or run a server. If you
  want a GUI on top of the JSONL logs, that's a great downstream
  project.

- **No telemetry, no usage reporting, no phone home.** Cardo runs
  entirely local. It writes only to `~/.cardo/` and the paths you
  explicitly pass it.

- **No automatic config changes.** Cardo never writes to its own
  config file based on usage patterns. If `~/.cardo/config.toml`
  changes, you changed it.

---

See also: [commands.md](commands.md), [design-notes.md](design-notes.md).
