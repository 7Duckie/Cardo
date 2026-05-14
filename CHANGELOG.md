# Changelog

All notable changes to Cardo will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-05-14

Initial public release. Cardo is a one-file, fifteen-command file manager
with a focus on safety: time-estimate preflights, dry-run mode everywhere,
installation-folder protection on destructive operations, optional trash
integration, and structured undo/restore logs for reversible actions.

### Added

#### Read-only commands
- `stats` — size and count breakdown by category, with optional HTML report
- `tree` — directory tree printout with depth limiting
- `search` — find files by name, size, or modification age
- `name-clash` — locate files with identical names spread across a tree

#### Destructive commands
- `copy` / `move` — copy or move files with optional pattern filtering
- `rename` — bulk-rename with regex, prefix, suffix, lowercase, uppercase,
  extension change, and numbered-template modes
- `organize` — sort files into category subfolders by type
- `dedupe` — find and remove duplicate files; three modes (quick = metadata
  only; standard = staged SHA-256; paranoid = staged SHA-256 + byte-by-byte
  verification before each deletion)
- `clean` — remove empty subdirectories
- `sync` — one-way mirror, additive by default, `--mirror` deletes extras

#### Recovery
- `undo` — reverse the most recent reversible run (move / rename / organize /
  clean) all-at-once
- `restore` — selectively reverse individual entries from any past run, via
  interactive picker, `--range`, or `--grep`
- `verify` — re-hash files and compare against the persistent hash cache;
  distinguishes silent corruption (content changed, metadata unchanged) from
  normal edits; suitable for periodic bit-rot checks

#### Configuration
- `config` — show, locate, or initialize `~/.cardo/config.toml`
- Config defaults flow through CLI flags: CLI > config > built-in
- Customizable file-type categories for `organize` and `stats`
- Configurable defaults for assume-yes, report, log, trash, dedupe mode,
  dedupe minimum size, dedupe worker count

#### Safety layer
- Time-estimate preflight before destructive operations
- Dry-run (`-n` / `--dry-run`) mode on every destructive command
- Installation-folder protection: refuses to operate inside `.app`,
  `.framework`, `.lrdata`, `.photoslibrary`, Adobe-style folders, Cinema 4D
  folders, JetBrains-style folders, and reverse-DNS layouts. Detects when
  the user has pointed cardo *at* an install folder and warns separately.
  Override via `--include-unsafe`.
- Trash mode (`--trash`) on dedupe, clean, sync; requires optional
  `send2trash` package
- Structured undo log at `~/.cardo/undo/` with partial-consumption tracking
- Per-run human-readable logs at `~/.cardo/logs/`
- Optional HTML reports at `~/.cardo/reports/` for dedupe / verify and
  read-only commands

#### Performance
- Parallel hashing via `ThreadPoolExecutor` for dedupe / verify, with
  configurable worker count (auto-detects up to `min(8, CPU)`)
- Persistent hash cache at `~/.cardo/cache/hashes.json` shared across runs
- Thread-safe `HashCache` and `ProgressBar`

#### Other
- `cardo-menu.command` — interactive macOS-friendly menu wrapper around the
  CLI; double-click to open in Terminal and walk through operations via
  numbered prompts. Includes a help-topic browser.

### Documentation

- README with overview, quickstart, command reference, and integration
  ideas
- `docs/installation.md` — installation paths and requirements
- `docs/commands.md` — full command reference (includes config reference)
- `docs/safety.md` — the protection system, trash, undo — what they do
  and why
- `docs/design-notes.md` — design decisions and tradeoffs

### License

MIT.

[1.0.0]: https://github.com/7Duckie/cardo/releases/tag/v1.0.0
