# Installation

Pick the install path that suits how you'll use cardo. All three end with
a working `cardo` command on your `PATH`.

## Option 1: Install from GitHub (recommended for most people)

```bash
pip install git+https://github.com/7Duckie/cardo.git
```

That's the whole thing. Then:

```bash
cardo --help                          # see all commands
cardo stats ~/Downloads               # try it on a folder
```

For trash support (recommended — files go to OS trash instead of being
permanently deleted), also install:

```bash
pip install send2trash
```

## Option 2: Run the single file directly

If you'd rather not install anything system-wide, grab the script and run
it with Python:

```bash
curl -O https://raw.githubusercontent.com/7Duckie/cardo/main/cardo.py
chmod +x cardo.py
python3 cardo.py --help
```

You can drop `cardo.py` anywhere on your `PATH` (e.g. `~/.local/bin/`) and
it'll behave like a normal command. To add trash support:

```bash
pip install --user send2trash
```

## Option 3: From source

For development work or to use unreleased changes:

```bash
git clone https://github.com/7Duckie/cardo.git
cd cardo
pip install -e ".[dev]"
```

This installs cardo in editable mode plus the development tools (ruff,
pytest). Changes you make to `cardo.py` take effect immediately.

## Option 4: The interactive menu (macOS double-click)

The repo also includes `cardo-menu.command` — a wrapper that presents a
numbered menu of operations, designed for double-click use in Finder.

1. Download `cardo.py` and `cardo-menu.command` to the same folder
2. Make `cardo-menu.command` executable: `chmod +x cardo-menu.command`
3. Double-click it in Finder — macOS opens it in Terminal

The menu walks you through prompts for paths and options, then calls the
underlying CLI. Use it when you'd rather not memorize flag names, or when
sharing cardo with someone less comfortable in a terminal.

## Verifying the install

```bash
cardo --help                    # should print the top-level usage
cardo --version                  # (if you're on a packaged install)
cardo stats /tmp                 # safe to run anywhere
```

If `cardo: command not found`, check that the install location is on your
`PATH`. For `pip install --user`, that's typically `~/.local/bin` on
Linux/macOS or `%APPDATA%\Python\Scripts` on Windows.

## Where cardo stores its data

All cardo state lives under `~/.cardo/`:

```
~/.cardo/
├── config.toml         # your settings (created by `cardo config init`)
├── cache/
│   └── hashes.json     # persistent hash cache for dedupe / verify
├── logs/               # human-readable per-run logs (when --log is used)
├── reports/            # HTML reports (when --report is used)
└── undo/               # JSONL undo logs for reversible operations
```

Nothing is created until you actually use the relevant feature — a
read-only `cardo stats` won't make any of these directories.

To remove cardo's state entirely (e.g. for testing, or to "factory reset"):

```bash
rm -rf ~/.cardo/
```

This won't uninstall the cardo binary, just clear its accumulated history.

## Uninstalling

```bash
# If you installed via pip:
pip uninstall cardo send2trash

# If you placed cardo.py manually:
rm /path/to/cardo.py

# To also remove cardo's state:
rm -rf ~/.cardo/
```

## Platform notes

- **macOS** — works out of the box. The `--trash` flag uses Finder Trash.
  Big Sur (11) and newer recommended.
- **Linux** — works on all major distros. The `--trash` flag uses the
  freedesktop trash spec (most desktop environments support it). On
  headless servers, install `send2trash` only if you actually want trash
  semantics; otherwise the absence of a desktop trash makes it less useful.
- **Windows** — works in PowerShell, Command Prompt, and Windows Terminal.
  The `--trash` flag uses the Recycle Bin. The `.command` menu wrapper
  doesn't auto-launch on double-click (that's macOS-specific); run it as
  `python cardo-menu.command` or write a similar `.bat` wrapper.
- **WSL / containers** — works fine; `--trash` may not have a usable
  destination depending on the environment.

## Troubleshooting

**"`cardo: command not found`"** — the install directory isn't on your
`PATH`. For pip user installs, add `~/.local/bin` to PATH.

**"`ModuleNotFoundError: No module named 'tomllib'`"** — you're on
Python 3.10 or older. Upgrade to 3.11+.

**"`! send2trash is not installed`"** when running with `--trash` — install
the optional dep: `pip install send2trash`.

**Cardo asks for confirmation every single time even on small ops** —
your config has `assume_yes = false` (the default). Pass `-y` per-command,
or set `assume_yes = true` in `~/.cardo/config.toml` if you want to skip
prompts globally.

**The protection prompt keeps appearing for folders that aren't actually
installed apps** — cardo's heuristic for "looks like an installed
application" is conservative but not perfect. Use `--include-unsafe` for
that specific run, or open an issue with the false-positive details so we
can refine the detection.

See also: [commands.md](commands.md) for what to do once installed,
[commands.md](commands.md#config) for the config file reference.
