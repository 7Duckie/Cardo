# Design notes

The rationale behind cardo's major decisions, written for people who
want to understand why it's the way it is — and for the next maintainer
who has to decide whether to change something.

This is deliberately a record of trade-offs rather than a tutorial. The
opinions here aren't final truths; they're the conclusions cardo's
authors reached based on the use cases they encountered. Reasonable
forks can disagree.

---

## Why one file

Cardo is one Python file, ~4,500 lines. The temptation to split it into
a package was constant. Why we didn't:

**Single-file is dramatically easier to install.** `curl -O` and you're
done. No virtualenv, no `pip`, no package conflicts, no surprises. A
single file is also auditable: a security-conscious user can read the
whole thing in an hour.

**Single-file is more honest about scope.** Cardo isn't a framework
trying to grow into a platform. It's a CLI utility with a specific job.
The "is this still in scope?" question gets a sharper answer when the
answer determines whether the file gets longer.

**The cost is real but small.** Yes, certain sections are long. Yes,
test discovery is slightly less clean. But the alternative — splitting
into `cardo/commands/dedupe.py`, `cardo/safety/protection.py`,
`cardo/io/cache.py` — creates layers that have to be threaded with
imports, and once that exists, the temptation to add abstractions
appears. Single-file forces every new function to justify its existence
in plain sight of the rest.

The boundary is "does the function make sense in this file." When that
answer ever becomes "no," it'll be time to split. So far the answer has
remained "yes" through many features.

### When to break this rule

If cardo grows a substantial domain-specific module — say, a built-in
indexing daemon, or a network sync protocol — that module probably
belongs in its own file. The current commands are all "walk files, do
thing to files," and that uniformity is why single-file works for them.

---

## Plan-then-execute

The strongest single design choice in cardo: every destructive command
builds a complete plan in memory before executing anything.

```python
def cmd_organize(args):
    plan = build_plan(...)        # nothing on disk changes yet
    if not preflight(plan): return
    if not protection_check(plan): return
    if args.dry_run: print_plan(plan); return
    execute(plan)                  # only now does anything change
```

This pattern is in every destructive command. Reasons:

**Dry-run becomes free.** When the plan is data, "what would happen?"
is just "print the plan" — no separate code path to maintain, no risk
of dry-run drifting out of sync with the real run.

**Protection becomes possible.** You can't check a plan you haven't
built. The protection layer's "skip the 8 protected actions, run the
1,239 safe ones" output requires having both numbers in hand before
starting.

**Accurate time estimates.** The plan contains file counts and byte
totals. Estimating "how long will this take?" reduces to looking at
those numbers and the operation type. No I/O-during-execution surprises.

**Failures don't leave the user confused.** If the plan was visible
before execution and the execution failed partway, the user knows what
was supposed to happen. Without plan-then-execute, a partial failure
produces uncertainty: "did it do the things I wanted before failing?"

**The cost is memory.** Plan entries are small (a few hundred bytes
each for a `(src, dst, size)` tuple), so even a million-entry plan is
under a gigabyte. We never hit a real workload where this was the
bottleneck.

### What this implies for new commands

If you're adding a new destructive command, the plan-then-execute
pattern is non-negotiable. Building the plan during execution
("streaming" the operation) defeats every safety mechanism above.

---

## Why so many preflights

A first-time user sometimes wonders why cardo asks for confirmation
twice — once for the time estimate, once for the protection summary. The
answer is that they're checking different things.

The **time-estimate prompt** is a sanity check on *scale and target*:
"you're about to organize 47,000 files under `/Volumes/Live SSD` — is
that what you meant?" It catches the "wrong directory" mistake.

The **protection prompt** is a sanity check on *content*: "of those
47,000 files, 8 are inside what looks like an Adobe installation. Do
you want to skip those?" It catches the "didn't realize there were
installed apps in there" mistake.

Combining them would lose the distinction. A user who answers "yes" to
"are you sure you want to do this big thing?" hasn't necessarily
answered "yes" to "do this big thing including the parts I marked as
risky." So they stay separate.

We considered a single prompt that listed both. It became harder to
read, especially on small-plan cases where the protection summary would
add noise to an otherwise trivial confirmation. Two prompts at the cost
of one extra keypress turned out to be the simpler design.

---

## The installation-folder protection: how it evolved

This is worth telling because it's the largest single design decision
in cardo, and the trigger was concrete.

### Stage 1 — Protection only inside `dedupe`

Originally, only `dedupe` knew about installed-application content. It
needed to, because dedupe might otherwise reclaim "duplicates" that
were actually separate copies of the same shared library inside two
different `.app` bundles. The detection lived inside `_dedupe_find_install_folders`
and was used solely by that one command.

The other destructive commands — `move`, `rename`, `organize`, `clean`,
`sync` — had no such check. They were considered "the user's
responsibility": you'd typed the path, you knew what was there.

### Stage 2 — The incident

A `clean --trash` run pointed at the root of a working SSD started
trashing empty preset folders inside Adobe InDesign 2026, Cinema 4D
2024, Motion.app, MagicQ, and Lightroom catalogs. The files were
recoverable from the OS trash, but if it had been a full `--include-unsafe
clean` rather than a typo-equivalent, the apps would have been broken
until reinstalled.

The root cause was structural: dedupe's protection was a *command-level*
feature when it should have been a *toolkit-level* one. Every
destructive command had the same need; only one had the implementation.

### Stage 3 — Lifting protection into a shared layer

The fix:

1. Hoist the detection machinery (`UNSAFE_PATH_SEGMENTS`,
   `find_install_folders`, `looks_like_install_folder`) out of
   `_dedupe_find_install_folders` and into a shared layer
2. Add `is_protected_path()` as the directory-aware variant (the
   original `classify_file()` was file-oriented and didn't fit `clean`'s
   directory operations)
3. Add `partition_safe_protected()` to split any plan into safe and
   protected actions
4. Add `confirm_protection_skip()` to standardize the summary +
   confirmation prompt
5. Wire all destructive commands through this layer

The result: every destructive command now has the same protection
behavior. The cost of adding a protection check to a new command is
~5 lines of code calling the shared helpers.

### Why "skip protected actions, ask once" instead of "abort if any"

Considered: "if any action would touch protected content, abort
entirely." Rejected because the most common case is a tree that's
mostly user files with a few stray installed apps: a `~/Downloads`
that happens to contain `installer.dmg` (already extracted into an
`.app`), or a `/Volumes/Backup` that has both user data and old app
backups. "Abort if any" would force the user to physically rearrange
their tree before running cardo, which is the opposite of helpful.

Considered: "silently skip protected actions." Rejected because the
protection is informational as well as protective — the user should
know that cardo found installed apps in the tree. Silent skipping is
the wrong half of "make the tool's understanding of the tree visible."

The chosen behavior — "skip protected, run safe, ask once before doing
either" — gets you the protection without forcing tree restructuring,
and it surfaces the protection so the user can confirm cardo's mental
model.

### The detection heuristic itself

Cardo's "looks like an installed application" detection combines
several signals (`Plug-Ins` subfolder, `License.txt`, reverse-DNS
children, versioned product name, binary-heavy contents). Any two of
them triggers detection.

This is deliberately fuzzy. A strict allowlist of known apps would
never catch novel applications. A strict pattern (e.g. "any folder
containing both `.app` and a reverse-DNS child") would miss the long
tail. The two-of-many heuristic catches the cases that matter without
needing constant maintenance for new apps.

False positives happen (a project folder with `License.txt` and
`Resources/` and a `.lproj` will trip it). The remedy is
`--include-unsafe` for that run, and an issue if the pattern is
reusable enough to refine.

---

## Safety as opt-out, not opt-in

Every safety feature in cardo is **on by default** and requires an
explicit flag to disable:

| Feature | On by default? | How to disable |
| :--- | :--- | :--- |
| Time-estimate prompt | Yes | `-y` per-run or `assume_yes = true` config |
| Protection check | Yes | `--include-unsafe` per-run (no config) |
| Undo log | Yes | `--no-undo` per-run (no config) |
| Confirmation on protection prompt | Yes | `-y` per-run |
| Dry-run mode | No (off) | `-n` to enable |
| Trash mode | No (off) | `--trash` per-run or `trash = true` config |

The pattern is consistent: things that protect the user from mistakes
are on; things that change the operation's character are off until
asked for.

The two exceptions are deliberate:

- **Trash** is off by default because it would require `send2trash`,
  and cardo's promise is "works with just Python stdlib." A user who
  wants trash can install the package and toggle the config setting in
  one minute. We thought about making `send2trash` a required
  dependency. The decision against it: someone running cardo in a
  container or on a minimal system shouldn't be blocked from using it.
- **Dry-run** is off because it's a per-decision choice, not a global
  setting. There's no plausible scenario where "always dry-run" makes
  sense.

### Why some safety flags aren't config-controllable

`--include-unsafe` and `--no-undo` are CLI-only on purpose. They
disable safety mechanisms; setting them globally would mean a machine
running cardo had its protection silently absent. That's a
configuration that should be impossible to enter accidentally — every
unsafe run requires a deliberate keystroke.

This is why the config-file documentation has a "things that aren't in
the config file" section. It's not laziness; those omissions are the
feature.

---

## Undo: structured logs as the recovery primitive

Most CLI tools that mutate the filesystem don't offer undo. Why cardo
does, and how:

### What makes undo possible

The set of reversible operations is small: `move`, `rename`,
`organize` (which is `move` underneath), `clean` (rmdir branch only).
What they have in common: the inverse of the operation can be
reconstructed from the operation's parameters alone, with no stored
content.

- `move src → dst` is reversed by `move dst → src`. No content needed.
- `rename old → new` is reversed by `rename new → old`. No content
  needed.
- `rmdir <path>` is reversed by `mkdir <path>`. The fact that the
  directory was empty when removed is recoverable: recreate it empty.

Anything that destroys content can't be reversed without storing the
content somewhere, and that's a different and much larger feature.
Cardo doesn't attempt it.

### Why JSONL

The undo logs are JSONL (JSON Lines) — one JSON object per line, with
a `_meta` header on the first line. Reasons:

- **Append-friendly.** Writing during a long-running operation is just
  "flush a line." No re-parsing the existing file. No partial-write
  problems if the run is killed.
- **Human-readable.** `cat`, `grep`, `less`, `jq` all work. A user
  debugging cardo's behavior doesn't need a special tool.
- **Streaming-parseable.** Reading entries one-at-a-time uses constant
  memory regardless of run size.
- **Self-documenting.** Each entry has its `op` field; the format is
  obvious from looking at a single line.

The alternative was a single binary file (Python pickle, sqlite). Both
were rejected: pickle is fragile across Python versions; sqlite would
make cardo depend on `sqlite3` for a feature that doesn't need a
database.

The one drawback of JSONL: rewriting the meta header at end-of-run
requires reading the whole file and writing it back. We accept this
because (a) it's a one-time cost per run, (b) the files are small,
and (c) the alternative — separate metadata file — was uglier.

### Why partial consumption

The original `undo` was all-or-nothing: reverse the whole most recent
run, mark the log fully consumed, done. Simple, but it didn't fit a
real use case: "I just organized 200 files and want to put 5 of them
back."

The naive answer: "undo everything, then re-do 195 moves manually."
That's terrible UX. So `restore` was added — a selective per-entry
undo — and the undo log was extended to track partial consumption.

The mechanism: the `_meta` header gains an `undone_entries` list of
indices that have been consumed. `restore` adds to this list when it
reverses entries. `undo` skips indices already in the list. When all
indices are consumed (by any combination of `undo` and `restore`),
the full `undone: true` flag flips.

This pattern keeps `undo` and `restore` independent: they're not
two implementations of the same thing, they're two access patterns
into the same data. Either can be used alone, or both can be used on
the same log without conflict.

### Why undo isn't itself undoable

We considered making `cardo undo` reversible — write its own undo log
so that "undo my undo" works. Decided against it:

- The user can re-run the original command if they want to undo their
  undo. That's both simpler and clearer about what's happening.
- Chained undo / redo gets confusing fast. "I undid an undo of an
  organize" is a state nobody enjoys reasoning about.
- The implementation would have been complicated by the partial-
  consumption mechanism. An undo of an undo would need to know which
  entries to "un-consume."

The chosen behavior — `undo` is not itself undoable — keeps the mental
model linear. There's always exactly one path to a desired state.

---

## Why `sync` has a deliberately limited undo

`sync` is the one destructive command that doesn't get full undo
support. The reasons are worth spelling out, because the question
comes up.

`sync` does three things:

1. **Copies new files** from source to destination → reversible (just
   delete the new files)
2. **Updates existing files** in destination from source → not
   reversible (the old content is gone)
3. **Deletes extras** from destination (only in `--mirror` mode) → not
   reversible unless `--trash` was set

Item 2 is the killer. To reverse a sync update, cardo would need to
have stored the destination's pre-update content somewhere. That's a
"safety net" feature with substantial storage cost — potentially
doubling the size of every sync.

The honest answer: routine sync runs go through `--trash` for the
deletion side (item 3); the update side (item 2) relies on the user
having a backup elsewhere. This is the same answer rsync gives.

We could record copies (item 1) in the undo log even when updates and
deletes aren't reversible. We chose not to, because a half-reversible
log is more confusing than no log at all — the user would expect undo
to work for the run and discover halfway through that only some
entries reverse.

If a future version adds opt-in pre-update backups (`--keep-old-as
.cardo-backup-<timestamp>`), that's the right place to add real undo
for sync. Without it, the limitation is honest.

---

## Parallel hashing

`dedupe` and `verify` are the only commands that parallelize. Why
threading (not multiprocessing), and how.

### Threading vs multiprocessing

Hashing is dominated by I/O — reading the file from disk — with a thin
layer of CPU work on top. Python's GIL doesn't prevent I/O-bound
threads from running concurrently: when one thread is waiting on
`read()`, another can run. So threading gives most of the speedup with
none of multiprocessing's overhead (fork costs, IPC, harder error
handling).

If hashing were CPU-bound, multiprocessing would beat threading. It
isn't, even with SHA-256, because SHA-256 in CPython actually runs in
C (`hashlib` is a thin wrapper around OpenSSL or its equivalent). The
GIL releases during the C-side work. So threading wins on simplicity
without losing significant performance.

### Why `min(8, CPU)` as the auto default

Two reasons to cap parallel workers:

- **Diminishing returns on most disks.** A modern NVMe handles a
  handful of concurrent readers before sequential I/O patterns degrade
  to random patterns and throughput tanks. Eight is a safe ceiling
  across SSD families.
- **Spinning rust hates parallelism.** On HDDs, 8 concurrent readers
  produce 8× the seek pressure, slower than serial. Users on HDDs
  should pass `--workers 1` explicitly.

The auto-detect formula `min(8, CPU)` is conservative on the high end
(big servers don't get 32 workers automatically) and forgiving on the
low end (single-core systems still get 1 worker). Power users can
override with `--workers N`.

### Lock contention

Two pieces of cardo are accessed from multiple threads: `HashCache`
(workers put computed hashes; main reads them) and `ProgressBar`
(workers update byte progress).

Both use a simple `threading.Lock()` wrapped around their internal
state. Lock contention is negligible because:

- Cache writes are infrequent (one per file, typically batched at end
  of run)
- Progress bar updates serialize through the lock but the operation
  inside is tiny (an arithmetic + a print)

We considered lock-free alternatives (atomic counters, per-thread
buffers merged at end). The complexity wasn't worth the negligible
speedup.

---

## The hash cache

`~/.cardo/cache/hashes.json` stores
`{path → {size, mtime, sha256}}`. Used by `dedupe` to skip rehashing
unchanged files and by `verify` to compare current hashes against
known-good baselines.

### Why one big JSON file

Alternatives considered:

- **SQLite** — adds a dependency on `sqlite3` (technically stdlib but
  not always built) and a query language for a simple key-value lookup
- **One file per hashed file** — much slower (millions of file system
  operations on big trees)
- **Pickled dict** — version-fragile, opaque, not human-readable

JSON wins on simplicity + readability. The cost is that on first read,
the whole file is parsed into memory. For a million cached entries
this is a few hundred milliseconds — acceptable for a tool that's
about to hash files at megabytes per second.

If cache size ever becomes a real problem, the right answer is
per-directory caches (one JSON file in each `.cardo-cache/` next to
the data). Not built yet because nobody has hit the limit.

### Freshness via (size, mtime)

The cache treats `(size, mtime)` as a freshness key: if both match the
current file's stat, the cached hash is trusted. If either differs,
the cache misses and the hash is recomputed.

This is the same trick rsync uses. It's wrong in exactly one case:
silent corruption (content changed, mtime didn't). That case is
exactly what `verify` exists to catch — `verify` deliberately ignores
the cache and re-hashes regardless of metadata.

We considered hashing-by-default with mtime as a hint. Rejected on
performance grounds: defeating the cache means every dedupe run reads
every byte. Routine dedupe runs are common enough that this would be
felt.

### Mtime tolerance

When `verify` compares its newly-computed hash against the cached one,
and the hashes differ, it classifies the file based on metadata:

- Size and mtime match → **CORRUPTED** (alarming)
- Size or mtime differ → **Modified** (normal edit)

But mtime comparison is fuzzy: `touch -d "@<timestamp>"`, FAT32 storage,
rsync without `--modify-window`, and many other paths drop sub-second
precision. Strict equality would mis-classify a restored file as
corrupted.

The 2-second tolerance is a calibrated balance: tight enough to still
catch genuine edits (real edits change mtime by far more than 2
seconds; nothing legitimate changes mtime by exactly 0-2 seconds and
also corrupts the content), loose enough to absorb precision loss.

---

## Dependency philosophy

Cardo's runtime dependencies, in full:

```
(none — Python 3.11 stdlib only)
```

Optional:

```
send2trash  (for --trash flag support)
```

That's it. The philosophy:

**Every dependency is a cost.** It's a thing that can break
installation, a thing the user has to trust, a thing that has to be
audited. Stdlib code is "free" in this sense; PyPI packages aren't.

**The single optional dep is justified by its standalone value.**
`send2trash` is small, well-maintained, and does the cross-platform
trash thing nothing in stdlib does. If we could have done it in
~200 lines of Python ourselves, we would have; we couldn't reasonably
match `send2trash`'s OS-specific quirks.

We considered adding dependencies for:

- **Progress bars (`tqdm`).** Rejected — `ProgressBar` is ~50 lines
  and does exactly what cardo needs.
- **Tables (`rich`, `tabulate`).** Rejected — cardo's output is
  intentionally plain; no need for the formatting power these libs
  provide.
- **TOML parsing.** Used to need `toml` or `tomli`; Python 3.11's
  `tomllib` made this stdlib. The 3.11+ minimum exists in part because
  of this.

The 3.11+ requirement is the one place where cardo's dependency
philosophy bit users on older systems. We accept this trade because
the alternative — running on 3.8+ and bundling a TOML parser — would
add a dependency and code complexity for a feature that's stdlib in
3.11.

---

## What cardo deliberately doesn't build

A few features come up regularly in discussions and have been
deliberately not implemented. The reasoning is worth recording so the
conversation doesn't have to be reopened every time.

### Two-way sync

`sync` is strictly one-way. Two-way sync requires conflict resolution:
what happens when the same file is modified on both sides? Every real
two-way sync tool (Syncthing, Resilio, Dropbox) has substantial
machinery for this — version vectors, conflict files, UI for
resolution. Cardo doesn't have UI for resolution, and adding it would
double the surface area of the command.

If you need two-way sync, use a real sync tool. Cardo's one-way sync
is for "I have a source-of-truth and a backup" workflows.

### Network operations

Cardo only operates on the local filesystem. No SSH, no S3, no
WebDAV. The reasoning:

- Each protocol has its own auth, error model, retry semantics
- A "remote-aware cardo" would be a different program; it'd share
  almost nothing with the local version
- Standard tools (`rsync`, `rclone`, `aws s3 sync`) already exist for
  these jobs

If you want remote operations, point cardo at a locally-mounted
remote (NFS, SMB, sshfs). Cardo doesn't know it's a remote, and it
doesn't need to.

### Indexing daemon

A background process that watches selected folders and maintains an
index for fast lookup. Tempting but big:

- Daemon lifecycle management (cross-platform: launchd, systemd,
  Windows services)
- IPC for the CLI to query the index
- Reconciliation when the daemon was stopped during file changes

Cardo's hash cache is the closest thing — it incrementally builds an
index across runs without needing a daemon. If you want a real
indexing daemon, that's a great downstream project; the existing cache
gives it a head start.

### Compression / encryption

Out of scope. Use `tar`, `gzip`, `gpg`, etc. before or after cardo's
operations.

### Backup rotation policies

"Keep daily backups for a week, weekly for a month, monthly forever"
is a recurring request. It's a real need, but the policy logic is
complex enough to be its own tool. Cardo's primitives (`sync`,
`organize`, `clean`) compose into rotation scripts; a built-in
rotation engine would be a substantial new feature.

### A GUI

Cardo is a CLI. The `cardo-menu.command` interactive wrapper exists
for users who want guided prompts, but it's still terminal-based.

A real GUI (Electron, Qt, native macOS) would be a different project.
The CLI structure (subcommands, structured logs, exit codes) makes it
easy to build a GUI on top — but that GUI should be its own repo, not
bolted onto cardo.

### Telemetry / usage reporting

None. Cardo never phones home. The only network calls in cardo are
ones the user explicitly invokes (e.g. if they pipe `cardo` output
into a script that uploads to a backup service).

### Config auto-discovery / migration

Cardo reads exactly `~/.cardo/config.toml`. No `XDG_CONFIG_HOME`
fallback, no `~/.config/cardo/`, no `/etc/cardo.toml`. One config per
user, in a predictable place.

We considered XDG support. The decision against it: simplicity beats
convention when the convention has unclear wins. A user wanting their
config in `~/.config/cardo/` can symlink it.

---

## Things that almost happened

A few features made it close to being built before being talked down:

### "Vacuum" command

A combined dedupe + clean: find duplicates, remove them, then clean
empty parents in one pass. Almost made it in. Talked out of because:

- The combined output is hard to read; it's better to see dedupe
  results, decide what to do, then clean separately
- Composing two existing commands with a shell pipeline does this
  already: `cardo dedupe X -r -y && cardo clean X -y`
- Adding it would create a fourth name for an operation users can
  describe two ways

### "Watch" mode

Run a command repeatedly when the target tree changes (e.g.
`cardo organize ~/Downloads --watch`). Rejected because:

- Repeated runs are typically the wrong solution. The right one is
  to invoke cardo from a file-system trigger (launchd `WatchPath`,
  `fswatch`, systemd path units)
- Watch-mode adds significant complexity (debouncing, event filtering,
  graceful shutdown) that's not in cardo's wheelhouse

### Per-command argv subdirectories in `~/.cardo/`

The undo logs were going to be organized as
`~/.cardo/undo/organize/YYYY-MM-DD.jsonl` instead of
`~/.cardo/undo/YYYY-MM-DD_organize.jsonl`. Rejected because the
single-directory layout makes "show me recent runs across all
commands" trivial; the subdirectory layout would require globbing
multiple directories.

### Multiple sync targets

`cardo sync SRC DST1 DST2 DST3 …` to fan out to several destinations
in one run. Rejected because:

- The protection check has to run per-destination, which complicates
  the preflight UX
- A shell loop (`for dst in DST1 DST2 DST3; do cardo sync SRC $dst; done`)
  does the same thing with clearer semantics

### Verbose mode (`-v`)

A flag that increases output detail. Cardo's output is already verbose
in the right places (per-file moves, plan summaries). Rejected because
adding `-v` would invite "add more output to `-v` mode" creep.

---

## The cardo name

The original project was called `filemgr`. It worked as a working
title but had problems:

- Generic enough to be confusing in search results
- Not memorable
- The abbreviated form ("file manager") competes with every GUI file
  manager ever made

The rename came late in development. Names considered:

- `fmgr` — even worse than `filemgr`
- `fsbroom` — cute but tries too hard
- `tidy` — taken (in CPAN, in npm, as a static-site generator)
- `cardo` — Roman main street, organizing spine of a town

The Roman-cardo metaphor lands: cardo is the principal road from which
the rest of the town is navigated. A file manager that organizes the
"spine" of your filesystem is the same idea applied to bytes. The
word is also rare enough to be searchable, short enough to type, and
clean across registries (npm, PyPI, brew, GitHub).

The rename happened all at once — every reference to `filemgr` in the
code, the storage paths (`~/.filemgr/` became `~/.cardo/`), the
argparse `prog=`, every user-facing string. A clean break is easier to
review than a slow renaming.

---

See also: [safety.md](safety.md) for the protection mechanisms in
operation; [commands.md](commands.md) for the user-facing reference;
the source of `cardo.py` itself for inline comments on specific
implementation choices.
