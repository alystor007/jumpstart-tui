#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 alystor007
"""
run_command.py — the jumpstart-tui launcher (standalone, no curses).

Reads the saved commands from ~/.config/jumpstart/commands.json and
spawns one detached: its own session (survives the TUI/shell exit),
stdin from /dev/null, stdout+stderr to a log file:
    ~/.local/state/jumpstart/logs/<name>-<timestamp>.log

Usage:
  python3 run_command.py <name>    # start a saved command, print pid + log
  python3 run_command.py --list    # list commands ('*' = selected)
"""

import json
import os
import re
import subprocess
import sys
import time

# Under sudo, $HOME is /root — use the invoking user's home (SUDO_HOME)
# so the config path is the same with and without sudo.
_HOME = os.environ.get("SUDO_HOME") or os.path.expanduser("~")
CONFIG_DIR = os.path.join(_HOME, ".config", "jumpstart")
COMMANDS_FILE = os.path.join(CONFIG_DIR, "commands.json")
LOG_DIR = os.path.join(_HOME, ".local", "state", "jumpstart", "logs")


def load_commands() -> tuple[dict, str]:
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


def list_commands() -> int:
    """Print the saved commands ('*' = selected)."""
    commands, selected = load_commands()
    if not commands:
        print(f"(no saved commands — {COMMANDS_FILE})")
        return 0
    for name, v in commands.items():
        mark = "*" if name == selected else " "
        desc = v.get("desc", "").strip() or "(no description)"
        print(f"{mark} {name} — {desc}")
    return 0


def start(name: str) -> int:
    """Spawn the saved command detached; print pid + log path."""
    commands, _selected = load_commands()
    if name not in commands:
        print(f"unknown command '{name}' — see --list", file=sys.stderr)
        return 2
    cmd = (commands[name].get("cmd") or "").strip()
    if not cmd:
        print(f"command '{name}' has no cmd — fix {COMMANDS_FILE}", file=sys.stderr)
        return 2
    os.makedirs(LOG_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    log_path = os.path.join(LOG_DIR, f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.log")
    logf = open(log_path, "w")
    try:
        # bash -c so pipes, && and env vars work; start_new_session puts the
        # process in its own session so it outlives this launcher (and the TUI).
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        logf.close()   # the child holds its own fd copy
    print(f"started '{name}' pid={proc.pid}")
    print(f"log={log_path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if args else 2)
    if args[0] == "--list":
        sys.exit(list_commands())
    if len(args) > 1:
        print("usage: run_command.py <name> | --list", file=sys.stderr)
        sys.exit(2)
    sys.exit(start(args[0]))
