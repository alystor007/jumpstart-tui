#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 alystor007
"""
jumpstart-tui — a lazydocker-style TUI for launching saved shell commands.

Commands live in ~/.config/jumpstart/commands.json (name, description, raw
shell command — run through `bash -c` so pipes, && and env vars work).
Execution is fire-and-forget: run_command.py spawns the command detached
(own session, stdin=/dev/null, stdout+stderr to a log file) and returns
immediately. Follow the process in docker/lazydocker or tail the log.

Keys:
  ↑ / ↓ .. move through the command list
  ← / → .. move the cursor inside the wizard's text fields
  Enter   -> Run the highlighted command (detached)
  n       -> Define + save a new command (central dialog)
  e       -> Edit the highlighted command
  d       -> Delete the highlighted command (y/N confirm)
  t       -> Cycle theme (saved between runs)
  ?       -> Help overlay
  q / Esc -> Quit
"""

import curses
import json
import os
import re
import subprocess
import sys
import textwrap

# ============================================================
#  SCRIPTS
# ============================================================

# The launcher lives next to this TUI — move them together, no config needed.
_HERE = os.path.dirname(os.path.abspath(__file__))
RUN_SCRIPT = os.path.join(_HERE, "run_command.py")

# ============================================================
#  COMMANDS
# ============================================================
# Single source of truth: commands.json.
# Shape: { "selected": "postgres", "postgres": {"desc": "...", "cmd": "..."} }

# Under sudo, $HOME is /root — use the invoking user's home (SUDO_HOME)
# so the config path is the same with and without sudo.
_HOME = os.environ.get("SUDO_HOME") or os.path.expanduser("~")
CONFIG_DIR = os.path.join(_HOME, ".config", "jumpstart")
COMMANDS_FILE = os.path.join(CONFIG_DIR, "commands.json")


def load_state() -> tuple[dict, str]:
    """Read commands.json. Returns (commands, selected_name)."""
    try:
        with open(COMMANDS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    selected = data.pop("selected", "")
    commands = {k: v for k, v in data.items() if isinstance(v, dict)}
    if selected not in commands:
        selected = next(iter(commands), "")
    return commands, selected


def save_commands(commands: dict, selected: str) -> bool:
    """Write commands.json — the selected name lives inside it.

    Returns True on success, False if the write could not be made (a failed
    write must never be silent: it would otherwise drop the command from
    disk while it still shows in the TUI).
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {"selected": selected}
        data.update(commands)
        with open(COMMANDS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


# ============================================================
#  THEMES
# ============================================================
# Pair 1 = ok/active, 2 = err/inactive, 3 = header, 4 = body.
# 256-color themes fall back to "dark" on terminals without 256 colors.

# "btn" = accent for the option buttons ([x], [n], ...) in the hint
# line, so the keys read as pressable buttons. Pair 6.
THEMES = {
    "dark":      {"fg": [curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_WHITE], "bg": curses.COLOR_BLACK, "btn": curses.COLOR_YELLOW},
    "light":     {"fg": [curses.COLOR_BLUE,  curses.COLOR_RED, curses.COLOR_MAGENTA, curses.COLOR_BLACK], "bg": curses.COLOR_WHITE, "btn": curses.COLOR_BLUE},
    "matrix":    {"fg": [curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN], "bg": curses.COLOR_BLACK, "btn": curses.COLOR_GREEN},
    "solarized": {"fg": [64, 166, 172, 252],  "bg": 235, "btn": 178},
    "gruvbox":   {"fg": [142, 167, 215, 223], "bg": 235, "btn": 216},
    "nord":      {"fg": [108, 173, 179, 252], "bg": 235, "btn": 117},
    "dracula":   {"fg": [46, 203, 190, 252],  "bg": 235, "btn": 226},
    # omarchy.org palette (Tokyo Night base + Omarchy green accent).
    # "transparent": -1 = the terminal's actual default fg/bg (needs
    # use_default_colors()), no bkgd fill — terminal shows through.
    "transparent": {"fg": [114, 124, 146, 111], "bg": -1, "btn": 118, "transparent": True},
}

THEME_FILE = os.path.join(CONFIG_DIR, "theme")


def next_theme(current: str) -> str:
    names = list(THEMES)
    return names[(names.index(current) + 1) % len(names)]


def apply_theme(stdscr, name: str) -> str:
    """Init color pairs for `name` and set the window background;
    returns the theme actually applied."""
    t = THEMES.get(name, THEMES["dark"])
    if max([*t["fg"], t["bg"], t["btn"]]) >= curses.COLORS:
        t, name = THEMES["dark"], "dark"
    if t.get("transparent"):
        # -1 only means "terminal default" once this flag is set.
        curses.use_default_colors()
    for i, fg in enumerate(t["fg"]):
        curses.init_pair(i + 1, fg, t["bg"])
    if t.get("transparent"):
        # No background: pair 5 = the terminal's own default colors,
        # and we skip bkgd() so its background shows through.
        curses.init_pair(5, -1, -1)
        curses.init_pair(6, t["btn"], -1)
        return name
    # Pair 5 = window background: whole-screen bg follows the theme.
    # (Pair 0 is the terminal default and cannot be re-init'd on
    # Python >= 3.14 / ncurses 6.5+, hence a dedicated pair.)
    curses.init_pair(5, t["fg"][3], t["bg"])
    # Pair 6 = option-button accent (the [keys] in the hint line).
    curses.init_pair(6, t["btn"], t["bg"])
    stdscr.bkgd(" ", curses.color_pair(5))
    return name


def draw_hint(stdscr, row, col, text, width):
    """Render a hint line with bracketed keys ([x], [n], ...) in the
    theme's button accent, so they read as pressable buttons."""
    if row >= stdscr.getmaxyx()[0] - 1:   # very short screen: skip the line
        return
    key_attr = curses.color_pair(6) | curses.A_BOLD
    c = col
    for seg in re.split(r"(\[[^\]]+\])", text):
        if not seg:
            continue
        if c >= width:          # nothing left on this row: stop cleanly
            break
        attr = key_attr if seg.startswith("[") else curses.color_pair(4)
        stdscr.addnstr(row, c, seg, width - c, attr)
        c += len(seg)


def load_theme() -> str:
    try:
        with open(THEME_FILE) as f:
            name = f.read().strip()
    except OSError:
        return "dark"
    return name if name in THEMES else "dark"


def save_theme(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass


# ============================================================
#  ACTIONS
# ============================================================

def run_selected(name: str) -> tuple[int, str]:
    """Ask run_command.py to spawn the saved command detached.
    Returns (rc, stdout). The launcher prints 'started ... pid=N' and
    'log=PATH' on success; stderr carries the failure reason."""
    r = subprocess.run(
        ["python3", RUN_SCRIPT, name],
        capture_output=True, text=True,
    )
    return r.returncode, (r.stdout or r.stderr).strip()


def prepend_log(log: str, entry: str) -> str:
    """Prepend an entry, keeping one line per entry."""
    return entry if not log else entry + "\n" + log


# ============================================================
#  COMMAND WIZARD
# ============================================================

def _prompt_key(stdscr) -> str | tuple[str, str] | None:
    """Read one key and resolve it into a line-editor action: 'esc',
    'enter', 'backspace', 'delete', 'left', 'right', 'home', 'end', or
    ('char', ch). Arrow keys arrive as keypad constants (KEY_LEFT, ...) or
    as raw ESC [ C/D sequences; both are decoded here so they can never
    corrupt the text buffer."""
    c = stdscr.getch()
    if c == -1:
        return "timeout"
    if c in (10, 13, curses.KEY_ENTER):
        return "enter"
    if c in (8, 127, curses.KEY_BACKSPACE, curses.KEY_DC):
        return "backspace"
    # Keypad mode (on by default under curses.wrapper) delivers arrow keys
    # as constants, not escape sequences — decode them before the "plain
    # key" check so they move the cursor instead of being dropped.
    if c == curses.KEY_LEFT:
        return "left"
    if c == curses.KEY_RIGHT:
        return "right"
    if c == curses.KEY_HOME:
        return "home"
    if c == curses.KEY_END:
        return "end"
    if c != 27:
        if 32 <= c < 256:
            return ("char", chr(c))
        return None
    # Escape: either a bare Esc (cancel) or the start of a key sequence.
    stdscr.timeout(30)
    c2 = stdscr.getch()
    stdscr.timeout(-1)
    if c2 not in (ord("["), ord("O")):
        return "esc"
    c3 = stdscr.getch()
    if c3 == ord("C"):
        return "right"
    if c3 == ord("D"):
        return "left"
    if c3 == ord("H"):
        return "home"
    if c3 == ord("F"):
        return "end"
    if c3 == ord("3"):
        stdscr.getch()   # consume '~'
        return "delete"
    if c3 == ord("1"):
        stdscr.getch()
        return "home"
    if c3 == ord("4"):
        stdscr.getch()
        return "end"
    if c3 == -1:
        return None     # split sequence (27, '[' then timeout): ignore, never cancel
    return "esc"


def _main_getch(stdscr) -> int | str:
    """Read one key for the main screen. Returns a curses key code (or -1)
    for plain keys, and "up"/"down" for arrow keys — decoded from both the
    keypad constants and raw ESC [ A/B sequences, so ↑/↓ work with or
    without keypad(). Left/right arrive as KEY_LEFT/KEY_RIGHT (keypad on)
    or ESC [ C/D (keypad off) and both map to "noop". A bare Esc arrives as 27; a split arrow sequence
    (27, '[' then timeout) is dropped, never treated as Esc."""
    c = stdscr.getch()
    if c != 27:
        return c
    stdscr.timeout(30)
    c2 = stdscr.getch()
    stdscr.timeout(100)
    if c2 not in (ord("["), ord("O")):
        return 27
    c3 = stdscr.getch()
    if c3 == ord("A"):
        return "up"
    if c3 == ord("B"):
        return "down"
    if c3 in (ord("C"), ord("D")):
        return "noop"   # left/right: no function in the main view
    if c3 == -1:
        return -1   # split sequence: ignore
    return 27       # unrecognized sequence: treat as Esc


def _edit_loop(stdscr, initial: str, render) -> str | None:
    """Shared line-editor loop. `render(text, cur)` draws the full current
    state (the wizard redraws its whole box; it owns the layout).
    Returns the stripped text on Enter, None on Esc. The buffer is
    unbounded — render() decides which slice is visible. Arrows move the
    cursor only; Backspace/Delete change the text."""
    text = initial
    cur = len(initial)
    render(text, cur)
    try:
        stdscr.timeout(-1)
        while True:
            act = _prompt_key(stdscr)
            if act == "timeout":
                continue
            if act == "esc":
                return None
            if act == "enter":
                return text.strip()
            if act == "backspace":
                if cur > 0:
                    text = text[:cur - 1] + text[cur:]
                    cur -= 1
            elif act == "delete":
                if cur < len(text):
                    text = text[:cur] + text[cur + 1:]
            elif act == "left":
                cur = max(0, cur - 1)
            elif act == "right":
                cur = min(len(text), cur + 1)
            elif act == "home":
                cur = 0
            elif act == "end":
                cur = len(text)
            elif act is not None:   # ("char", ch)
                ch = act[1]
                text = text[:cur] + ch + text[cur:]
                cur += len(ch)
            render(text, cur)
    finally:
        stdscr.timeout(100)


def _name_error(name: str, commands: dict, old_name: str) -> str:
    """Validate a (possibly new) command name. Returns '' when OK."""
    if not name:
        return "name cannot be empty"
    if name == "selected":
        return "name 'selected' is reserved"
    if name != old_name and name in commands:
        return f"'{name}' already exists"
    return ""


def draw_wizard_box(stdscr, sh, sw, top, left, bw, bh, title, values, active_i,
                    error, active_text, active_cur):
    """Draw the wizard box: border, title, the three fields (the active one
    with the live editor text + reverse-video cursor), hint, error line."""
    labels = ("Command name", "Description", "Shell command")
    keys = ("name", "desc", "cmd")
    attr = curses.color_pair(4)
    H, W = stdscr.getmaxyx()
    for r in range(top, top + bh):
        if r < H - 1:
            stdscr.addnstr(r, left, " " * bw, bw)
    for r in range(top, top + bh):
        if r >= H - 1:
            break
        stdscr.addch(r, left, "┌" if r == top else ("└" if r == top + bh - 1 else "│"))
        stdscr.addch(r, left + bw - 1, "┐" if r == top else ("┘" if r == top + bh - 1 else "│"))
    stdscr.addnstr(top, left + 1, "─" * (bw - 2), bw - 2, attr)
    stdscr.addnstr(top + bh - 1, left + 1, "─" * (bw - 2), bw - 2, attr)
    stdscr.addnstr(top, left + 2, title, bw - 4, curses.color_pair(3) | curses.A_BOLD)
    for fi, (label, key) in enumerate(zip(labels, keys)):
        row = top + 2 + fi
        if row >= H - 1:
            break
        is_active = fi == active_i
        text = active_text if is_active else values[key]
        lbl_attr = curses.color_pair(4) | curses.A_BOLD if is_active else curses.color_pair(4)
        stdscr.addnstr(row, left + 2, label + ":", bw - 4, lbl_attr)
        tcol = left + 2 + len(label) + 1
        twidth = max(1, bw - 4 - len(label) - 1)
        if is_active:
            # visible slice around the cursor (the field scrolls, not the box)
            start = max(0, min(active_cur - 1, len(active_text) - twidth)) if len(active_text) > twidth else 0
            pre = active_text[start:active_cur]
            post = active_text[active_cur + 1: start + twidth]
            stdscr.addnstr(row, tcol, pre, twidth)
            ch = active_text[active_cur] if active_cur < len(active_text) else " "
            stdscr.addch(row, tcol + len(pre), ch, curses.A_REVERSE)
            stdscr.addnstr(row, tcol + len(pre) + 1, post, twidth - len(pre) - 1)
        else:
            stdscr.addnstr(row, tcol, text, twidth)
    stdscr.addnstr(top + 5, left + 2, "[Enter] next field    [Esc] cancel", bw - 4, attr)
    if error:
        stdscr.addnstr(top + bh - 1, left + 1, error, bw - 2, curses.color_pair(2))


def command_wizard(stdscr, sh, sw, commands, old_name="", desc="", cmd="") -> tuple[str, str, str] | None:
    """Central overlay for defining (old_name='') or editing a command.
    Returns (name, desc, cmd) on save, None on Esc."""
    bw = max(10, min(60, sw - 2))
    bh = 8
    top = max(0, (sh - bh) // 2)
    left = max(1, (sw - bw) // 2)
    title = f"Edit: {old_name}" if old_name else "New command"
    values = {"name": old_name, "desc": desc, "cmd": cmd}
    keys = ("name", "desc", "cmd")
    error = ""
    i = 0
    while i < len(keys):
        key = keys[i]

        def render(text, cur, key=key, i=i):
            draw_wizard_box(stdscr, sh, sw, top, left, bw, bh, title, values, i,
                            error, text, cur)

        res = _edit_loop(stdscr, values[key], render)
        if res is None:
            return None
        if key == "name":
            err = _name_error(res, commands, old_name)
            if err:
                error = err
                continue   # re-prompt the name field
            error = ""
        values[key] = res
        i += 1
    return values["name"], values["desc"], values["cmd"]


def confirm(stdscr, sh, sw, question: str) -> bool:
    """Small centered y/N confirm box. True on y/Y, False on n/N/q/Esc."""
    bw = max(10, min(sw - 2, len(question) + 8))
    top = max(1, (sh - 4) // 2)
    left = max(1, (sw - bw) // 2)
    attr = curses.color_pair(4)
    H, W = stdscr.getmaxyx()

    def draw():
        for r in range(top, top + 3):
            if r < H - 1:
                stdscr.addnstr(r, left, " " * bw, bw)
        for r, tc, bc in ((top, "┌", "┐"), (top + 1, "│", "│"), (top + 2, "└", "┘")):
            if r < H - 1:
                stdscr.addch(r, left, tc, attr)
                stdscr.addch(r, left + bw - 1, bc, attr)
        if top < H - 1:
            stdscr.addnstr(top, left + 1, "─" * (bw - 2), bw - 2, attr)
        if top + 2 < H - 1:
            stdscr.addnstr(top + 2, left + 1, "─" * (bw - 2), bw - 2, attr)
        if top < H - 1:
            stdscr.addnstr(top, left + 2, question, bw - 4, curses.color_pair(4) | curses.A_BOLD)
        if top + 1 < H - 1:
            stdscr.addnstr(top + 1, left + 2, "[y] yes    [n] no", bw - 4, attr)
        stdscr.refresh()

    draw()
    stdscr.timeout(-1)
    try:
        while True:
            c = stdscr.getch()
            if c in (ord("y"), ord("Y")):
                return True
            if c in (ord("n"), ord("N"), ord("q"), 27):
                return False
    finally:
        stdscr.timeout(100)


# ============================================================
#  BANNER  —  "JUMP" (header) + "START" (body)
# ============================================================
# Each row is (left, right): left = "JUMP" (drawn in the header color),
# right = "START" (drawn in the body color). Built from a 4-col block
# font so it stays aligned in monospace.
BANNER = [
    ("████ ██   ████", "███████ █ ██████"),
    (" ███ ███ ███ █", "█   ███ ██ █ ██"),
    (" ███ ██ █ ████", " ██ ████████ ██"),
    ("█ ██ ██   ██  ", "   █ ███ ██ █ ██"),
    ("██ ████   ██  ", "████ ███ ██ █ ██"),
]

BANNER_ROWS = len(BANNER)   # 5

HELP_TEXT = [
    "  ↑ / ↓ .. move through the command list",
    "  Enter .. run the highlighted command (detached)",
    "  n ...... define + save a new command",
    "  e ...... edit the highlighted command",
    "  d ...... delete the highlighted command (y/N)",
    "  t ...... cycle color theme",
    "  q / Esc . quit",
    "  fields  . arrows move, Bksp/Del edit, Enter next, Esc cancel",
    "  ? ...... toggle this help",
    "",
    "  Commands: ~/.config/jumpstart/commands.json",
    "  Output:   ~/.local/state/jumpstart/logs/<name>-<ts>.log",
]


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    theme = apply_theme(stdscr, load_theme())

    commands, selected = load_state()

    help_overlay = False
    log = ""
    # UI tick: getch returns -1 after 100ms -> smooth redraws.
    stdscr.timeout(100)

    # Flicker-free rendering: each frame is drawn into stdscr's virtual
    # buffer (erase + redraw); refresh() diffs it against the physical
    # screen and pushes only the changed cells (steady state: none).
    while True:
        hint = ("[↑↓] move  [Enter] run  [n] new  [e] edit  [d] delete  "
                "[t] theme  [q] quit  [?] help")
        log_lines = (log or "(no action yet)").splitlines()

        # --- render ------------------------------------------------------
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        sh, sw = max(1, h - 1), max(1, w - 1)   # safe bounds

        def border(win, H, W):
            """Single-line frame on the screen edge (drawn last: owns the
            corners and any content bleed). Needs 4x10 minimum."""
            if H < 4 or W < 10:
                return
            attr = curses.color_pair(4)
            win.addch(0, 0, "┌")
            win.addch(H - 2, 0, "└")
            win.addch(0, W - 2, "┐")
            win.addch(H - 2, W - 2, "┘")
            win.addnstr(0, 1, "─" * (W - 3), W - 3, attr)
            win.addnstr(H - 2, 1, "─" * (W - 3), W - 3, attr)
            for r in range(1, H - 2):
                win.addnstr(r, 0, "│", 1, attr)
                win.addnstr(r, W - 2, "│", 1, attr)

        PAD = 4
        TOP = 1
        def divider(row):
            if row < sh:
                stdscr.addnstr(row, 0, ("─" * sw), sw)

        # ASCII title: "JUMP" (header) + "START" (body) — clamped to the
        # safe height AND width so small screens never get out-of-bounds
        # writes (addnstr's n is "max chars", not "max columns").
        for r, (left, right) in enumerate(BANNER):
            if TOP + r >= sh:
                break
            stdscr.addnstr(TOP + r, PAD, left, max(0, sw - PAD),
                           curses.color_pair(3) | curses.A_BOLD)
            # +2: breathing space between "JUMP" and "START"
            col2 = PAD + len(left) + 2
            if col2 < sw:
                stdscr.addnstr(TOP + r, col2, right, sw - col2, curses.color_pair(4))
        divider(TOP + BANNER_ROWS)

        # --- button hints ---
        hint_row = TOP + BANNER_ROWS + 1
        draw_hint(stdscr, hint_row, PAD, hint, sw)

        # --- main area: two columns — names (left) + details (right) ---
        divider(hint_row + 1)
        main_row = hint_row + 2
        names = list(commands)
        if selected not in names:
            selected = names[0] if names else ""
        sel_idx = names.index(selected) if selected in names else -1
        # The log panel is anchored to the bottom of the screen (its header
        # sits 2 rows above the footer, leaving room for up to 2 lines);
        # the main list area gets whatever rows are left above it.
        log_h = max(0, min(3, (sh - 2) - main_row))
        fit = max(0, (sh - 2) - main_row - log_h)   # rows for the list area
        last_used = main_row
        if not help_overlay and names:
            left_w = max(10, min(30, sw // 4))
            right_col = PAD + left_w + 3   # one blank column after the divider
            list_fit = max(0, fit - 2)    # header + empty spacer row + list rows
            # header row: column titles (header color, like the "JUMP" word)
            if fit >= 1:
                stdscr.addnstr(main_row, PAD, "NAME", left_w, curses.color_pair(3) | curses.A_BOLD)
                stdscr.addnstr(main_row, right_col, "DESCRIPTION / COMMAND", max(0, sw - right_col),
                               curses.color_pair(3) | curses.A_BOLD)
            # vertical divider between the name list and the details column:
            # runs from the header row down through the whole list area
            div_col = PAD + left_w + 1
            for i in range(fit):
                if main_row + i < h - 1:
                    stdscr.addch(main_row + i, div_col, "\u2502", curses.color_pair(4))
            # keep the highlighted row visible: scroll the list, not the screen
            top_idx = max(0, min(sel_idx - list_fit + 1, len(names) - list_fit)) if sel_idx >= list_fit else 0
            for i in range(list_fit):
                idx = top_idx + i
                if idx >= len(names):
                    break
                name = names[idx]
                is_sel = name == selected
                mark = "*" if is_sel else " "
                row_attr = curses.color_pair(1) | curses.A_BOLD if is_sel else curses.color_pair(4)
                num = idx + 1   # 1-based position, survives list scrolling
                stdscr.addnstr(main_row + 2 + i, PAD, f"{mark} {num:>2} {name}", left_w, row_attr)
            # right column: description + wrapped command of the highlighted one
            # (only when the list area actually has rows to draw into)
            lines = []
            if list_fit > 0:
                entry = commands[selected]
                desc = (entry.get("desc") or "").strip()
                stdscr.addnstr(main_row + 2, right_col, desc or "(no description)",
                               max(0, sw - right_col), curses.color_pair(4))
                cmd = (entry.get("cmd") or "").strip()
                wrapped = textwrap.fill(cmd, width=max(10, sw - right_col - 1)) if cmd else "(empty command)"
                lines = wrapped.splitlines()
                for i, line in enumerate(lines[:max(0, list_fit - 1)]):
                    stdscr.addnstr(main_row + 3 + i, right_col, line, max(0, sw - right_col), curses.color_pair(4))
                # clip marker only when there's a spare row below the desc
                if list_fit >= 2 and len(lines) > list_fit - 1:
                    stdscr.addnstr(main_row + list_fit + 1, right_col, "…", max(0, sw - right_col), curses.color_pair(4))
            # content height = header+spacer+names vs header+spacer+detail block
            hdr = min(fit, 2)   # header row, plus the empty spacer row when it fits
            list_h = hdr + min(len(names), list_fit)
            detail_h = hdr + (1 + min(len(lines), max(0, list_fit - 1)) if list_fit > 0 else 0)
            last_used = main_row + max(list_h, detail_h) - 1
        elif not help_overlay:
            stdscr.addnstr(main_row, PAD, "(no commands — press n to add one)",
                           sw, curses.color_pair(2))
            last_used = main_row
        if help_overlay:
            for i, line in enumerate(HELP_TEXT[:max(0, fit)]):
                stdscr.addnstr(main_row + i, PAD, line, sw, curses.color_pair(4))
            last_used = main_row + max(0, min(len(HELP_TEXT), fit) - 1)
        last_used = min(max(last_used, main_row), sh - 2)

        # --- log: anchored to the bottom of the screen ---
        if log_h > 0:
            log_top = sh - 1 - log_h      # "Log:" header; up to 2 lines below
            # bottom line of the list area, pinned to the log section: it sits
            # one row above the "Log:" header, not where the content ends
            bottom_line = log_top - 1
            if main_row < bottom_line < log_top:
                divider(bottom_line)      # always, even with few rows of content
            stdscr.addnstr(log_top, PAD, "Log:", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            for i, line in enumerate(log_lines[:log_h - 1]):
                stdscr.addnstr(log_top + 1 + i, PAD, line, sw, curses.color_pair(4))

        stdscr.addnstr(sh - 1, 0, ("─" * sw), sw)   # footer (never last row)
        border(stdscr, h, w)
        note = f" theme: {theme} [t]   selected: {selected or '-'} "
        if len(note) > w:
            note = "…" + note[1 - w:]   # keep the right (most useful) part
        stdscr.addnstr(sh - 1, max(0, w - len(note)), note, min(len(note), w),
                       curses.color_pair(4) | curses.A_BOLD)

        # frame complete: refresh diffs the virtual buffer against the
        # physical screen and rewrites only the cells that changed
        # (steady state: none -> no flicker, no churn).
        stdscr.refresh()

        # --- input -------------------------------------------------------
        c = _main_getch(stdscr)
        if c in (-1, "noop"):    # timeout / left-right arrow: no action, redraw
            continue
        if c in (27, ord("q")):
            if help_overlay:
                help_overlay = False
            else:
                break
            continue
        if help_overlay:
            if c in (ord("?"), ord("h")):
                help_overlay = False
            continue
        if c in ("up", curses.KEY_UP):
            if names and sel_idx > 0:
                selected = names[sel_idx - 1]
                save_commands(commands, selected)
        elif c in ("down", curses.KEY_DOWN):
            if names and sel_idx < len(names) - 1:
                selected = names[sel_idx + 1]
                save_commands(commands, selected)
        elif c == 10 or c == 13 or c == curses.KEY_ENTER:
            if selected in commands:
                rc, out = run_selected(selected)
                if rc == 0:
                    pid = logp = ""
                    for line in out.splitlines():
                        if line.startswith("pid=") or "pid=" in line:
                            m = re.search(r"pid=(\d+)", line)
                            if m:
                                pid = m.group(1)
                        if line.startswith("log="):
                            logp = line[len("log="):].strip()
                    log = prepend_log(log, f"[run] '{selected}' started pid={pid} log={logp}")
                else:
                    log = prepend_log(log, f"[err] run '{selected}' rc={rc}\n{out}")
        elif c == ord("n"):
            res = command_wizard(stdscr, sh, sw, commands)
            if res:
                name, desc, cmd = res
                commands[name] = {"desc": desc, "cmd": cmd}
                selected = name   # a fresh command becomes the highlighted one
                if not save_commands(commands, selected):
                    log = prepend_log(log, f"[err] could not write '{name}' to "
                                           + COMMANDS_FILE + " (not saved)")
                else:
                    log = prepend_log(log, f"[command] saved '{name}'")
        elif c == ord("e"):
            if selected in commands:
                old = selected
                res = command_wizard(stdscr, sh, sw, commands, old_name=old,
                                     desc=commands[old].get("desc", ""),
                                     cmd=commands[old].get("cmd", ""))
                if res:
                    new_name, desc, cmd = res
                    new_entry = {"desc": desc, "cmd": cmd}
                    if new_name != old:
                        rebuilt = {}
                        for k, v in commands.items():
                            if k == old:
                                rebuilt[new_name] = new_entry
                            else:
                                rebuilt[k] = v
                        commands.clear()
                        commands.update(rebuilt)
                    else:
                        commands[new_name] = new_entry
                    selected = new_name
                    if not save_commands(commands, selected):
                        log = prepend_log(log, f"[err] could not write '{selected}' to "
                                               + COMMANDS_FILE + " (not saved)")
                    else:
                        if new_name != old:
                            log = prepend_log(log, f"[command] renamed '{old}' -> '{new_name}'")
                        else:
                            log = prepend_log(log, f"[command] updated '{selected}'")
        elif c == ord("d"):
            if selected in commands:
                target = selected
                shown = target if len(target) <= 20 else target[:17] + "..."
                if confirm(stdscr, sh, sw, f"Delete '{shown}'?"):
                    del commands[target]
                    names = list(commands)
                    selected = names[sel_idx] if sel_idx < len(names) else \
                        (names[-1] if names else "")
                    save_commands(commands, selected)
                    log = prepend_log(log, f"[command] deleted '{target}', "
                                           f"selected '{selected or '(none)'}'")
        elif c in (ord("?"), ord("h")):
            help_overlay = True
        elif c == ord("t"):
            theme = apply_theme(stdscr, next_theme(theme))
            save_theme(theme)
            log = prepend_log(log, f"[theme] switched to {theme}")


# ============================================================


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    sys.exit(0)
