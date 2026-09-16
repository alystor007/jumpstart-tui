# Jumpstart TUI

A lazydocker-style terminal UI for saving and launching shell commands —
`docker run` one-liners, build jobs, service scripts. Commands are stored as
plain entries (name, description, raw shell command) and launched
**detached**: the TUI never blocks, and you follow the process in
docker/lazydocker or by tailing its log.

Built with the same TUI architecture as
[nvidia-tui-overclocker](https://github.com/alystor007/Nvidia-TUI-overclocker):
flicker-free diff rendering, switchable themes, session action log.

## Screenshot

![Jumpstart TUI](jumpstart-tui.png)

## Files

| File | Purpose |
|------|---------|
| `jumpstart_tui.py` | The TUI (run this). |
| `run_command.py` | Standalone launcher — the TUI calls it; also usable from the shell. |
| `test_jumpstart.py` | Self-contained test suite (fake curses screen, no display needed). |

## Requirements

- Linux (or any POSIX box) with a terminal supporting 256 colors
  (8 colors works — 256-color themes fall back to the dark palette)
- Python 3.10+, curses (standard library — **zero third-party dependencies**)

## Usage

Run the TUI:

```
python3 jumpstart_tui.py
```

Standalone launcher:

```
python3 run_command.py --list       # list saved commands ('*' = selected)
python3 run_command.py <name>       # start a saved command detached
```

## Main screen

The list is two columns: **NAME** (left, `*` marks the selected command,
numbered, scrolls to keep the highlight visible) and **DESCRIPTION / COMMAND**
(right, wrapped). A `Log:` panel anchored to the bottom shows the session's
action log; the footer shows the current theme and selected command.

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the selection (persisted) |
| `Enter` | Run the selected command (detached) |
| `n` | Define + save a new command (wizard) |
| `e` | Edit the selected command (wizard, can rename) |
| `c` | Clone the selected command (prefilled as `<name> copy`) |
| `d` | Delete the selected command (y/N confirm) |
| `l` | Copy the newest log file path to the clipboard |
| `t` | Cycle color theme (persisted) |
| `?` / `h` | Help overlay (any key / Esc closes it) |
| `q` / `Esc` | Quit |

`Esc` is snappy: ncurses `escdelay` is capped at 25 ms and arrow keys are
decoded both as keypad constants and raw escape sequences, so they work with
or without keypad mode and a bare Esc never corrupts text.

### The command wizard

`n` / `e` / `c` open the central wizard with three fields: **Command name**,
**Description**, **Shell command**.

- Labels are padded to one shared column, so all field text aligns.
- The three fields are drawn as tonal **text boxes** (a lighter-than-background
  fill) so it is obvious what is editable; the cursor and any selection are
  reverse-video.
- The **shell command field wraps across two lines**. The buffer is a single
  logical line, hard-split at the field width (the same split is used active
  and inactive, so nothing reflows when focus moves); when the command is
  longer than two rows the field shows a sliding window that keeps the cursor
  line visible.
- Field flow: `←`/`→` move the cursor, `Home`/`End` jump, `Bksp`/`Del`
  delete, `Enter` accepts the field and advances (name → description →
  command → save), `Esc` cancels the whole wizard.
- `Ctrl+V` toggles in-field selection: `←`/`→` extend it, typing or `Bksp`
  replaces/deletes the selection, any other key collapses it.
- Validation happens per field on `Enter`: an empty name, a duplicate name,
  or the reserved name `selected` re-prompts the field with the error shown
  in red on the box's bottom border.
- The action hint `[^v] select  [Enter] next  [Esc] cancel` is anchored to the
  bottom of the box, with bracketed keys in the theme's button accent — the
  same style as the main screen's `[n] [e] [d]…` keys.

### Themes

`t` cycles all eight; the choice persists between runs:

`dark`, `light`, `matrix`, `solarized`, `gruvbox`, `nord`, `dracula`,
`transparent` (terminal background shows through; the editable fields keep a
subtle gray fill so they still read as boxes).

## Data

One source of truth, created on first save:

```
~/.config/jumpstart/commands.json
```

```json
{
  "selected": "postgres",
  "postgres": {
    "desc": "Postgres 16 with volume",
    "cmd": "docker run -d --name pg16 -e POSTGRES_PASSWORD=*** -p 5432:5432 -v pg16_data:/var/lib/postgresql/data --restart unless-stopped postgres:16"
  }
}
```

The command is executed via `bash -c`, so pipes, `&&`, env vars and quoting
all work. Commands run in the directory the TUI was launched from (a
per-command `cwd` field is a candidate for a later version).

- Theme persists to `~/.config/jumpstart/theme`.
- Command output goes to `~/.local/state/jumpstart/logs/<name>-<timestamp>.log`.
- First run starts empty on purpose: there is no seeded command you could
  accidentally fire.
- Under `sudo`, both scripts honor `SUDO_HOME` so the config/log paths stay
  the same with and without sudo.

## Execution model

Pressing `Enter` calls `run_command.py <name>`, which spawns the command in
its own session (`start_new_session`, `stdin=/dev/null`, stdout+stderr → log)
and exits immediately. The TUI records the pid + log path in its session log
and is never blocked. To stop a detached process, manage it in the tool that
owns it (e.g. `docker stop <name>`) or `kill <pid>` from the TUI log line.

`l` copies the newest log path (by mtime, read from disk — works after a
restart) to the clipboard via **OSC 52** (no local tool needed, works over
SSH), falling back to `wl-copy` / `xclip` / `xsel` if the terminal rejects it.

## Development

- Tests: `python3 test_jumpstart.py` — self-contained (fake curses screen,
  no display needed), grid-level layout assertions included.
- Lint: `ruff check .` (config: `ruff.toml`, line-length 160, F/E rules)
- CI (`.github/workflows/ci.yml`): byte-compile + ruff + test suite on push/PR
- License: GPL-3.0-or-later
