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
  Enter   -> Run the selected command (detached)
  x       -> Open the Commands menu (scrolling: arrows, Enter selects)
  n       -> Define + save a new command
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


def command_summary(v: dict) -> str:
    """Compact one-line summary for the menu: the description, or a
    clipped command if the entry has none."""
    desc = (v.get("desc") or "").strip()
    if desc:
        return desc
    return (v.get("cmd") or "").strip()


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

def prompt(stdscr, sh: int, sw: int, label: str, default: str = "") -> str | None:
    """Blocking text prompt on the footer row. None on Esc."""
    stdscr.timeout(-1)
    curses.echo()
    stdscr.move(sh - 1, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(sh - 1, 0, f"{label} [{default}]: ", sw)
    stdscr.refresh()
    res = stdscr.getstr()
    curses.noecho()
    stdscr.timeout(100)
    raw = res[1] if isinstance(res, tuple) else res  # ncurses returns (n, b) or bare bytes
    text = raw.decode(errors="replace").strip()
    if text.startswith("\x1b"):   # Esc then Enter = cancel
        return None
    return text if text else default


def new_command(stdscr, sh: int, sw: int, commands: dict) -> tuple[str, str]:
    """Define and save a new command. Returns (name, log_msg)."""
    name = prompt(stdscr, sh, sw, "Command name", "mycmd")
    if not name:
        return "", "[command] cancelled"
    if name == "selected":
        return "", "[command] name 'selected' is reserved"
    if name in commands:
        return "", f"[command] '{name}' already exists"
    desc = prompt(stdscr, sh, sw, "Description")
    if desc is None:
        return "", "[command] cancelled"
    cmd = prompt(stdscr, sh, sw, "Shell command")
    if cmd is None:
        return "", "[command] cancelled"
    commands[name] = {"desc": desc, "cmd": cmd}
    return name, f"[command] saved '{name}'"


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
    "  Enter ... run the selected command (detached)",
    "  x ...... open the Commands menu (Esc/q close)",
    "  arrows . move the cursor inside the menu",
    "  Enter . select the cursor command + close menu",
    "  n ...... define + save a new command",
    "  d ...... delete the cursor command (menu open)",
    "  t ...... cycle color theme",
    "  q / Esc . quit",
    "  Esc+Enter cancels a prompt",
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
    menu_open = False
    menu_cursor = 0
    last_run: dict = {}   # {name, pid, log} of the most recent run
    log = ""
    # UI tick: getch returns -1 after 100ms -> smooth redraws.
    stdscr.timeout(100)

    # Flicker-free rendering: each frame is drawn into stdscr's virtual
    # buffer (erase + redraw); refresh() diffs it against the physical
    # screen and pushes only the changed cells (steady state: none).
    while True:
        if menu_open:
            hint = "[↑↓] move  [Enter] select  [n] new  [d] delete  [Esc/q] close"
        else:
            hint = "[Enter] Run [x] Commands [n] New [t] Theme [q] Quit [?] Help"

        log_lines = (log or "(no action yet)").splitlines()

        # --- render ------------------------------------------------------
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        sh, sw = max(1, h - 1), max(1, w - 1)   # safe bounds

        def border(win, H, W):
            """Single-line frame on the screen edge (drawn last: owns the
            corners and any content bleed). Needs 4x10 minimum."""
            # stdscr cannot write the true bottom row/col, so the frame
            # is drawn on the safe bounds (one cell in from the edge).
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

        PAD = 4     # horizontal indent (padding) for the banner block
        TOP = 1     # blank row above the banner
        def divider(row):
            if row < sh:
                stdscr.addnstr(row, 0, ("─" * sw), sw)

        # ASCII title: "JUMP" (header) + "START" (body)
        for r, (left, right) in enumerate(BANNER):
            stdscr.addnstr(TOP + r, PAD, left, sw, curses.color_pair(3) | curses.A_BOLD)
            # +2: breathing space between "JUMP" and "START"
            stdscr.addnstr(TOP + r, PAD + len(left) + 2, right, sw, curses.color_pair(4))
        divider(TOP + BANNER_ROWS)

        # --- status: selected command + its description ---
        status_row = TOP + BANNER_ROWS + 1
        stdscr.addnstr(status_row, PAD, "selected:", sw,
                       curses.color_pair(4) | curses.A_BOLD)
        sel_name = selected if selected in commands else ""
        stdscr.addnstr(status_row, PAD + 12, sel_name or "(none)", sw,
                       curses.color_pair(1) if sel_name else curses.color_pair(2), )
        desc = (commands.get(selected, {}).get("desc") or "").strip() if sel_name else ""
        stdscr.addnstr(status_row + 1, PAD, f"desc: {desc}" if desc else "desc: (none)",
                       sw, curses.color_pair(4))

        # --- button hints ---
        divider(status_row + 2)
        hint_row = status_row + 3
        draw_hint(stdscr, hint_row, PAD, hint, sw)

        # --- main area: commands menu, help, or the selected command ---
        divider(hint_row + 1)
        main_row = hint_row + 2
        available = max(0, (sh - 2) - (main_row + 1))   # rows left for main content
        last_used = main_row
        if menu_open:
            names = list(commands)
            menu_cursor = min(max(0, menu_cursor), max(0, len(names) - 1))
            # keep the cursor visible: scroll the list, not the screen
            top_idx = max(0, menu_cursor - (available - 5))
            visible = names[top_idx:]
            stdscr.addnstr(main_row, PAD, "Commands:  [↑↓] move  [Enter] select"
                                              "  [n] new  [d] delete", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            for i, name in enumerate(visible):
                is_cur = name == names[top_idx + i]
                mark = "*" if name == selected else " "
                row_attr = curses.color_pair(1) | curses.A_BOLD if (is_cur or mark == "*") \
                    else curses.color_pair(4)
                stdscr.addnstr(main_row + 1 + i, PAD + 1, f"{mark} {name}", sw, row_attr)
                stdscr.addnstr(main_row + 1 + i, PAD + 18, command_summary(commands[name]),
                               sw, curses.color_pair(4))
            if not names:
                stdscr.addnstr(main_row + 1, PAD + 1, "(no commands — press n)",
                               sw, curses.color_pair(2))
                last_used = main_row + 1
            else:
                last_used = main_row + min(len(visible), available - 1)
        elif not help_overlay:
            if sel_name:
                cmd = (commands[selected].get("cmd") or "").strip()
                wrapped = textwrap.fill(cmd, width=max(10, sw - PAD - 2)) \
                    if cmd else "(empty command)"
                lines = wrapped.splitlines()
                stdscr.addnstr(main_row, PAD, "Command:", sw,
                               curses.color_pair(4) | curses.A_BOLD)
                fit = available - 2   # leave the last line for the run info
                for i, line in enumerate(lines[:max(0, fit)]):
                    stdscr.addnstr(main_row + 1 + i, PAD + 1, line, sw, curses.color_pair(4))
                if len(lines) > max(0, fit):
                    stdscr.addnstr(main_row + fit, PAD + 1, "…", sw, curses.color_pair(4))
                last_used = main_row + max(0, fit)
                if last_run:
                    info = f"last run: '{last_run['name']}' pid={last_run['pid']} log={last_run['log']}"
                    stdscr.addnstr(last_used + 1, PAD + 1, info, sw, curses.color_pair(2))
                    last_used += 1
            else:
                stdscr.addnstr(main_row, PAD, "(no commands — press n to add one)",
                               sw, curses.color_pair(2))
                last_used = main_row
        # help overlay replaces the main area
        if help_overlay:
            for i, line in enumerate(HELP_TEXT[:available]):
                stdscr.addnstr(main_row + i, PAD, line, sw, curses.color_pair(4))
            last_used = main_row + min(len(HELP_TEXT), available) - 1
        last_used = min(last_used, sh - 2)

        # --- log (clamped: never overflows below the footer) ---
        divider(last_used + 1)
        log_top = last_used + 2
        if log_top < sh - 1:
            stdscr.addnstr(log_top, PAD, "Log:", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            fit = (sh - 2) - (log_top + 1)
            for i, line in enumerate(log_lines[:max(0, fit)]):
                stdscr.addnstr(log_top + 1 + i, PAD, line, sw, curses.color_pair(4))

        stdscr.addnstr(sh - 1, 0, ("─" * sw), sw)   # footer (never last row)
        border(stdscr, h, w)
        note = f" theme: {theme} [t]   selected: {sel_name or '-'} [x] "
        if menu_open:
            note += " [menu open]"
        stdscr.addnstr(sh - 1, max(0, w - len(note)), note, len(note),
                       curses.color_pair(4) | curses.A_BOLD)

        # frame complete: refresh diffs the virtual buffer against the
        # physical screen and rewrites only the cells that changed
        # (steady state: none -> no flicker, no churn).
        stdscr.refresh()

        # --- input -------------------------------------------------------
        c = stdscr.getch()
        if c == -1:        # timeout — just redraw
            continue
        if c in (ord("q"), 27):
            if menu_open or help_overlay:
                menu_open = False
                help_overlay = False
            else:
                break
            continue
        if menu_open:
            if c in (curses.KEY_UP,):
                menu_cursor = max(0, menu_cursor - 1)
            elif c in (curses.KEY_DOWN,):
                names = list(commands)
                if names:
                    menu_cursor = min(len(names) - 1, menu_cursor + 1)
            elif c == 10 or c == 13 or c == curses.KEY_ENTER:
                # Enter: select the cursor command and close the menu
                names = list(commands)
                if names:
                    selected = names[menu_cursor]
                    save_commands(commands, selected)
                    log = prepend_log(log, f"[command] selected '{selected}'")
                menu_open = False
            elif c == ord("n"):
                name, entry = new_command(stdscr, sh, sw, commands)
                if name:
                    if not save_commands(commands, selected or name):
                        log = prepend_log(log, f"[err] could not write '{name}' "
                                               "to " + COMMANDS_FILE + " (not saved)")
                    else:
                        menu_cursor = min(len(commands) - 1, max(0, menu_cursor))
                log = prepend_log(log, entry)
            elif c == ord("d"):
                names = list(commands)
                if names and menu_cursor < len(names):
                    target = names[menu_cursor]
                    del commands[target]
                    if selected == target:
                        selected = names[menu_cursor] if menu_cursor < len(names) else \
                            (names[-1] if names else "")
                        save_commands(commands, selected)
                        log = prepend_log(log, f"[command] deleted '{target}', "
                                               f"selected '{selected or '(none)'}'")
                    else:
                        save_commands(commands, selected)
                        log = prepend_log(log, f"[command] deleted '{target}'")
                    menu_cursor = min(menu_cursor, max(0, len(commands) - 1))
            continue
        if c == 10 or c == 13 or c == curses.KEY_ENTER:
            if sel_name:
                rc, out = run_selected(sel_name)
                if rc == 0:
                    pid = logp = ""
                    for line in out.splitlines():
                        if line.startswith("pid=") or "pid=" in line:
                            m = re.search(r"pid=(\d+)", line)
                            if m:
                                pid = m.group(1)
                        if line.startswith("log="):
                            logp = line[len("log="):].strip()
                    last_run = {"name": sel_name, "pid": pid, "log": logp}
                    log = prepend_log(log, f"[run] '{sel_name}' started pid={pid} log={logp}")
                else:
                    log = prepend_log(log, f"[err] run '{sel_name}' rc={rc}\n{out}")
        elif c == ord("x"):
            menu_open = True
            menu_cursor = min(max(0, list(commands).index(selected)), max(0, len(commands) - 1)) \
                if commands else 0
        elif c == ord("n"):
            name, entry = new_command(stdscr, sh, sw, commands)
            if name:
                if not save_commands(commands, name):
                    log = prepend_log(log, f"[err] could not write '{name}' "
                                           "to " + COMMANDS_FILE + " (not saved)")
                else:
                    selected = name   # a fresh command becomes the selected one
            log = prepend_log(log, entry)
        elif c in (ord("?"), ord("h")):
            help_overlay = not help_overlay
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
