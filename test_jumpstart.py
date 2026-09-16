import curses
import json
import os
import shutil
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="js-tui-")
os.environ["SUDO_HOME"] = TMP
sys.path.insert(0, ".")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import jumpstart_tui as jt  # noqa: E402  (import after curses mocking on purpose)
curses.start_color = lambda: None
curses.COLORS = 256
curses.init_pair = lambda p, f, b: p
curses.color_pair = lambda p: 0
curses.use_default_colors = lambda: None
jt.run_selected = lambda name: (0, "started\npid=42\nlog=/tmp/fake.log")
# clipboard stub: records every path asked to be copied (assert the exact arg)
CLIP = []
_real_copy = jt._copy_to_clipboard   # keep the real fn for the OSC 52 test
jt._copy_to_clipboard = lambda t: (CLIP.append(t), (True, "xclip"))[1]
curses.curs_set = lambda n: None

ESC = 27
LEFT, RIGHT = [ESC,91,68], [ESC,91,67]
HOME, END   = [ESC,91,72], [ESC,91,70]
DEL         = [ESC,91,51,126]
BSP, ENTER  = [127], [10]

def K(*parts):
    out = []
    for p in parts:
        if isinstance(p, str):
            out += [ord(c) for c in p]
        elif isinstance(p, int):
            out.append(p)
        else:
            out += list(p)
    return out

class FakeScr:
    def __init__(self, h, w, keys=()):
        self.h, self.w = h, w
        self.grid = [[" "] * w for _ in range(h)]
        self.keys = list(keys)
        self.ooobad = 0
    def getmaxyx(self): return (self.h, self.w)
    def erase(self): self.grid = [[" "] * self.w for _ in range(self.h)]
    def _put(self, r, c, ch):
        if 0 <= r < self.h and 0 <= c < self.w:
            self.grid[r][c] = ch
        else:
            self.ooobad += 1
    def addch(self, r, c, ch, *a): self._put(r, c, ch[0])
    def addnstr(self, r, c, s, n, *a):
        for i, ch in enumerate(s[:n]):
            self._put(r, c + i, ch)
    def refresh(self): pass
    def getch(self): return self.keys.pop(0) if self.keys else -1
    def timeout(self, t): pass
    def bkgd(self, *a): pass
    def keypad(self, *a): pass

jt.apply_theme = lambda s, n: "dark"
passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

# ---------- _edit_loop ----------
def edit(keys, initial=""):
    scr = FakeScr(24, 80, keys)
    return jt._edit_loop(scr, initial, lambda t, c, s: None)

r = edit(K("abc", ENTER))
check("edit: plain type", r == "abc", r)
r = edit(K("ab", LEFT, RIGHT, RIGHT, LEFT, ENTER))
check("edit: arrows move only", r == "ab", r)
r = edit(K("abc", LEFT, LEFT, "X", ENTER))
check("edit: insert middle", r == "aXbc", r)
r = edit(K("ab", LEFT, BSP, ENTER))
check("edit: backspace", r == "b", r)
r = edit(K("ab", LEFT, DEL, ENTER))
check("edit: delete", r == "a", r)
r = edit(K("ab", LEFT, LEFT, BSP, ENTER))
check("edit: bsp at start no-op", r == "ab", r)
r = edit(K("abc", END, "!", HOME, "#", ENTER))
check("edit: home/end", r == "#abc!", r)
long = "echo " + "x" * 300 + " | tee /var/log/x"
r = edit(K(long, ENTER))
check("edit: 300+ char integrity", r == long, r[:40])
r = edit(K("abc", [ESC]))
check("edit: esc cancels", r is None, r)
r = edit(K("a", -1, "b", ENTER))
check("edit: timeout continues", r == "ab", r)
# keypad mode (default under curses.wrapper): arrows arrive as KEY_* constants
r = edit(K("abc", curses.KEY_LEFT, curses.KEY_LEFT, "X", ENTER))
check("edit: keypad left", r == "aXbc", r)
r = edit(K("abc", curses.KEY_RIGHT, "X", ENTER))
check("edit: keypad right", r == "abcX", r)
r = edit(K("abc", curses.KEY_HOME, "Z", curses.KEY_END, "!", ENTER))
check("edit: keypad home/end", r == "Zabc!", r)
r = edit(K("a", [ESC, 91, -1], "b", ENTER))
check("edit: split esc ignored", r == "ab", r)

# ---------- selection (Ctrl+V) ----------
SEL = 22  # Ctrl+V
r = edit(K("hello", HOME, SEL, LEFT, LEFT, ENTER))
check("sel: no-op until extended", r == "hello", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, "Z", ENTER))
check("sel: extend then type replaces", r == "Zllo", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, BSP, ENTER))
check("sel: backspace removes selection", r == "llo", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, "X", ENTER))
check("sel: replace keeps following", r == "Xllo", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, LEFT, ENTER))
check("sel: arrow collapses, no delete", r == "hello", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, RIGHT, ENTER))
check("sel: extend past end clamps", r == "hello", r)
r = edit(K("hello", SEL, RIGHT, RIGHT, RIGHT, ENTER))
check("sel: extend from cursor", r == "hello", r)
r = edit(K("hello", SEL, LEFT, LEFT, "Q", ENTER))
check("sel: extend leftwards", r == "helQ", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, SEL, RIGHT, ENTER))
check("sel: second toggle clears selection", r == "hello", r)
r = edit(K("hello", HOME, SEL, RIGHT, RIGHT, ENTER))
check("sel: enter returns full text (selection is visual)", r == "hello", r)

# ---------- confirm ----------
def confirm(keys):
    scr = FakeScr(24, 80, keys)
    return jt.confirm(scr, 23, 79, "Delete 'x'?")
check("confirm: y", confirm(K("y")))
check("confirm: Y", confirm(K("Y")))
check("confirm: n", not confirm(K("n")))
check("confirm: N", not confirm(K("N")))
check("confirm: q", not confirm(K("q")))
check("confirm: esc", not confirm([ESC]))
check("confirm: other ignored", confirm(K("hjy")))

# ---------- wizard ----------
def wizard(keys, commands, **kw):
    scr = FakeScr(24, 80, keys)
    return jt.command_wizard(scr, 23, 79, commands, **kw)

cmds = {"a": {"desc": "", "cmd": "echo a"}, "b": {"desc": "", "cmd": "echo b"}}
r = wizard(K("new1", ENTER, "my desc", ENTER, "echo hi", ENTER), cmds)
check("wizard: create", r == ("new1", "my desc", "echo hi"), r)
r = wizard(K("selected", ENTER, "ok", ENTER, ENTER, "echo c", ENTER), cmds)
check("wizard: reserved name rejected", r == ("ok", "", "echo c"), r)
r = wizard(K("a", ENTER, "newa", ENTER, ENTER, "echo d", ENTER), cmds)
check("wizard: duplicate name rejected", r == ("newa", "", "echo d"), r)
r = wizard(K("new2", ENTER, "desc", [ESC]), cmds)
check("wizard: esc cancels", r is None, r)
# edit mode: name prefilled "a" -> Enter keeps it; desc prefilled "old" ->
# clear with backspaces then type; cmd prefilled "echo a" -> clear then type
r = wizard(K(ENTER) + K(*[" "] * 0, BSP * 3, "new desc", ENTER)
           + K(BSP * 6, "echo e", ENTER),
           cmds, old_name="a", desc="old", cmd="echo a")
check("wizard: edit keep name", r == ("a", "new desc", "echo e"), r)
# rename: type over the prefilled name (clear it first)
r = wizard(K(BSP * 1, "renamed", ENTER, BSP * 3, "d", ENTER, BSP * 6, "echo f", ENTER),
           cmds, old_name="a", desc="old", cmd="echo a")
check("wizard: edit rename", r == ("renamed", "d", "echo f"), r)

# ---------- wizard layout: bottom-anchored hint + 2-line cmd field ----------
# Geometry on FakeScr 24x80 (sh=23, sw=79): bw=60, bh=9, top=(23-9)//2=7,
# left=(79-60)//2=9; field rows: name=9, desc=10, cmd=11+12; hint=top+7=14;
# bottom border (= error row)=15. cmd: tcol=25 (left+2+len(label)+1,
# "Shell command" is 13 chars, colon appended), twidth=42 (bw-4-len-1).
def wiz_grid(keys, **kw):
    s = FakeScr(24, 80, keys)
    res = jt.command_wizard(s, 23, 79, cmds, **kw)
    return res, ["".join(row) for row in s.grid], s.ooobad

TCOL, TWIDTH = 25, 42
long_cmd = "curl -s http://example.com/api | jq . | head -n 50"
res, rows, oob = wiz_grid(K("n1", [ESC]), cmd=long_cmd)
check("wizlay: esc cancels (grid probe)", res is None, res)
check("wizlay: cmd label row", "Shell command:" in rows[11], repr(rows[11]))
# inactive cmd field wraps onto two rows (hard split at twidth)
check("wizlay: inactive cmd row 1", rows[11][TCOL:TCOL + TWIDTH] == long_cmd[:TWIDTH],
      repr(rows[11][TCOL:TCOL + TWIDTH]))
check("wizlay: inactive cmd row 2",
      rows[12][TCOL:TCOL + len(long_cmd) - TWIDTH] == long_cmd[TWIDTH:],
      repr(rows[12][TCOL:TCOL + len(long_cmd) - TWIDTH]))
# hint anchored to the bottom: one row above the bottom border (row 14),
# NOT on the old row 12 which now holds the second cmd line
check("wizlay: hint anchored at bottom",
      "[Enter] next" in rows[14] and "[Esc] cancel" in rows[14] and "select" not in rows[12],
      repr(rows[14]))
check("wizlay: no oob", oob == 0, oob)

# active cmd field: 2 visible rows, vertically scrolled so the cursor (at the
# end) is on the second row — Esc keeps the wizard open so the final frame
# still shows the box (Enter would return to the main view)
big_cmd = "echo " + "x" * 120 + " | tail"   # 132 chars -> 4 wrapped rows
res, rows, oob = wiz_grid(K("n3", ENTER, "d", ENTER, big_cmd, [ESC]))
check("wizlay: esc on cmd cancels (grid probe)", res is None, res)
nrows = (len(big_cmd) + TWIDTH - 1) // TWIDTH
start_row = min(len(big_cmd) // TWIDTH - 1, nrows - 2)   # cursor on last line
check("wizlay: active cmd scrolled (prev line)",
      rows[11][TCOL:TCOL + TWIDTH] == big_cmd[start_row * TWIDTH:(start_row + 1) * TWIDTH],
      repr(rows[11][TCOL:TCOL + TWIDTH]))
check("wizlay: active cmd scrolled (last line)",
      rows[12][TCOL:TCOL + TWIDTH] == (big_cmd[(start_row + 1) * TWIDTH:] + " " * TWIDTH)[:TWIDTH],
      repr(rows[12][TCOL:TCOL + TWIDTH]))
check("wizlay: long cmd no oob", oob == 0, oob)

# error lands on the bottom border row (row 15): type a reserved name,
# confirm it (error shows, name re-prompted), then Esc — last frame carries it
res, rows, oob = wiz_grid(K("selected", ENTER, [ESC]))
check("wizlay: error on bottom border row", "name 'selected' is reserved" in rows[15],
      repr(rows[15]))
check("wizlay: error no oob", oob == 0, oob)

# ---------- full main() ----------
os.makedirs(os.path.join(TMP, ".config", "jumpstart"), exist_ok=True)
seed = {"selected": "a",
        "a": {"desc": "first desc", "cmd": "echo a"},
        "b": {"desc": "second desc", "cmd": "echo b && sleep 1"},
        "c": {"desc": "", "cmd": "curl -s localhost | grep x"}}
json.dump(seed, open(jt.COMMANDS_FILE, "w"))

# phase 1: create + edit (check JSON immediately, before any delete)
keys1 = (
    [curses.KEY_DOWN, curses.KEY_DOWN]            # highlight c
    + K("n", "new1", ENTER, "nd", ENTER, "echo new1", ENTER)   # create
    + K("e", ENTER, BSP * 2, "nd2", ENTER, BSP * 9, "echo new1 --flag", ENTER)   # edit (keep name)
    + K("q", "q")                                 # quit
)
scr = FakeScr(24, 80, keys1)
jt.main(scr)
data = json.load(open(jt.COMMANDS_FILE))
check("main: new saved", "new1" in data)
check("main: edited desc", data.get("new1", {}).get("desc") == "nd2", data.get("new1"))
check("main: edited cmd", data.get("new1", {}).get("cmd") == "echo new1 --flag", data.get("new1"))
check("main: selected follows new", data.get("selected") == "new1", data.get("selected"))
check("main: no oob (phase 1)", scr.ooobad == 0, scr.ooobad)
grid = "\n".join("".join(r).rstrip() for r in scr.grid)
rows = ["".join(r) for r in scr.grid]
check("main: two-col details", "echo new1 --flag" in grid, "cmd not in grid")
# column headers + vertical divider between the name list and the details
check("main: column headers", any(r.lstrip("\u2502 ").startswith("NAME") for r in rows)
      and "DESCRIPTION / COMMAND" in grid, grid[:200])
# mirror the render math: sw = w-1; left_w = max(10, min(30, sw // 4)); div_col = PAD + left_w + 1
sw_t, PAD_t = scr.w - 1, 4
left_w_t = max(10, min(30, sw_t // 4))
div_col = PAD_t + left_w_t + 1
# locate the header row (the one whose left col starts with "NAME"); all other
# layout assertions are relative to it so they hold on any screen height
hdr_row = next((i for i, r in enumerate(rows) if r.lstrip("\u2502 ").startswith("NAME")), None)
check("main: header row found", hdr_row is not None, grid[:200])
# vertical divider runs from the header row down through the list area
check("main: vertical divider",
      hdr_row is not None and all(rows[r][div_col] == "\u2502" for r in range(hdr_row, hdr_row + 6)),
      [rows[r][div_col] for r in range(hdr_row, hdr_row + 6)] if hdr_row is not None else "no hdr")
# spacer row right under the headers: both columns empty (ignore the borders at
# col 0 and col w-2, which frame the whole screen)
sp = hdr_row + 1
right_border = scr.w - 2
check("main: empty row under headers",
      rows[sp][PAD_t:div_col] == " " * (div_col - PAD_t)
      and rows[sp][div_col + 1:right_border] == " " * (right_border - div_col - 1),
      repr(rows[sp]))
# right column not squashed: one blank column between divider and the first text
right_col_t = div_col + 2
check("main: right col has a gap",
      rows[sp + 1][div_col + 1] == " " and rows[sp + 1][right_col_t] != " ",
      repr(rows[sp + 1][div_col - 1:div_col + 6]))
# left column: 1-based numbers in front of each name
check("main: numbered names", " 1 a" in grid and " 2 b" in grid and " 3 c" in grid, grid[:300])
# log section anchored to the bottom: 2 rows of content right above the footer
# (FakeScr 24x80: sh=23, main_row=10, log_h=3 -> Log: at 19, lines at 20-21, footer 22)
row = [r.lstrip("\u2502 ") for r in rows]   # drop the left border
check("main: log header anchored to bottom", row[19].startswith("Log:"), repr(row[19]))
check("main: log lines sit right above footer",
      row[20].startswith("[command]") and row[21].startswith("[command]"),
      row[20:22])
check("main: footer selected", "selected: new1" in grid)
check("main: log lines", "[command] saved 'new1'" in grid
      and "[command] updated 'new1'" in grid)

# arrow keys in the MAIN view: raw ESC sequences, keypad constants, and the
# "right arrow must not quit / must not edit" guarantee
json.dump({"selected": "a", "a": {"desc": "d", "cmd": "echo a"},
           "b": {"desc": "d2", "cmd": "echo b"}}, open(jt.COMMANDS_FILE, "w"))
# escape sequences: [A=up [B=down [C=right [D=left (91='[')
scrA = FakeScr(24, 80, [27, 91, 66, 27, 91, 65, ord("q")])  # down, up, quit
jt.main(scrA)
dataA = json.load(open(jt.COMMANDS_FILE))
check("main: raw arrow seqs navigate", dataA.get("selected") == "a", dataA.get("selected"))
scrB = FakeScr(24, 80, [curses.KEY_DOWN, curses.KEY_UP, 27, 91, 67, ord("q")])
jt.main(scrB)
dataB = json.load(open(jt.COMMANDS_FILE))
check("main: keypad arrows + right-arrow noquit", dataB.get("selected") == "a",
      (dataB.get("selected"), dataB))
check("main: arrows no oob", scrA.ooobad == 0 and scrB.ooobad == 0)

# phase 2 (fresh state, selected 'a'): d+n cancel (a stays), d+y deletes a ->
# reselects names[0] = 'b'.
json.dump({"selected": "a",
           "a": {"desc": "first desc", "cmd": "echo a"},
           "b": {"desc": "second desc", "cmd": "echo b && sleep 1"},
           "c": {"desc": "", "cmd": "curl -s localhost | grep x"}},
          open(jt.COMMANDS_FILE, "w"))
keys2 = (
    K("d", "n")                                     # delete, cancel (a stays)
    + K("d", "y")                                   # delete, confirm (a gone, b highlighted)
    + K("q", "q")
)
scr2 = FakeScr(24, 80, keys2)
jt.main(scr2)
data2 = json.load(open(jt.COMMANDS_FILE))
names2 = [k for k in data2 if k != "selected"]
check("main: delete cancel kept then deleted",
      "a" not in names2 and set(names2) == {"b", "c"} and data2.get("selected") == "b",
      (names2, data2.get("selected")))
check("main: no oob (phase 2)", scr2.ooobad == 0, scr2.ooobad)
grid2 = "\n".join("".join(r).rstrip() for r in scr2.grid)
check("main: delete logged", "[command] deleted 'a'" in grid2, grid2[:200])
# highlight mark: "* name" starts at PAD=4
check("main: highlight rendered",
      any(r[4:6] == "* " for r in ["".join(x) for x in scr2.grid]))

# ---------- duplicate (D) ----------
check("dup: free name", jt._dup_name("a", {"a": {}}) == "a copy")
check("dup: bump past taken", jt._dup_name("a", {"a": {}, "a copy": {}, "a copy 2": {}}) == "a copy 3")
check("dup: different base", jt._dup_name("b", {"a": {}, "b": {}}) == "b copy")

json.dump({"selected": "a",
           "a": {"desc": "first desc", "cmd": "echo a"},
           "b": {"desc": "second desc", "cmd": "echo b"}}, open(jt.COMMANDS_FILE, "w"))
scrD = FakeScr(24, 80, K("c", ENTER, ENTER, ENTER, "q"))
jt.main(scrD)
dataD = json.load(open(jt.COMMANDS_FILE))
namesD = [k for k in dataD if k != "selected"]
check("main: duplicate created", set(namesD) == {"a", "b", "a copy"}, namesD)
check("main: duplicate copies fields",
      dataD["a copy"]["desc"] == "first desc" and dataD["a copy"]["cmd"] == "echo a",
      dataD.get("a copy"))
check("main: duplicate selected", dataD.get("selected") == "a copy", dataD.get("selected"))
check("main: duplicate no oob", scrD.ooobad == 0, scrD.ooobad)
gridD = "\n".join("".join(r) for r in scrD.grid)
check("main: duplicate logged", "[command] duplicated 'a' as 'a copy'" in gridD, gridD[-200:])

# name already taken -> bumped
json.dump({"selected": "a",
           "a": {"desc": "", "cmd": "echo a"},
           "a copy": {"desc": "", "cmd": "x"},
           "b": {"desc": "", "cmd": "echo b"}}, open(jt.COMMANDS_FILE, "w"))
scrD2 = FakeScr(24, 80, K("c", ENTER, ENTER, ENTER, "q"))
jt.main(scrD2)
dataD2 = json.load(open(jt.COMMANDS_FILE))
check("main: duplicate name bumped",
      set(k for k in dataD2 if k != "selected") == {"a", "a copy", "b", "a copy 2"},
      [k for k in dataD2 if k != "selected"])

# ---------- copy log path (l) ----------
# _latest_log_path picks the newest .log by mtime (None when dir absent/empty)
os.makedirs(jt.LOG_DIR, exist_ok=True)
old_log = os.path.join(jt.LOG_DIR, "a-20260101-000000.log")
new_log = os.path.join(jt.LOG_DIR, "b-20260102-000000.log")
for p in (old_log, new_log):
    with open(p, "w") as f:
        f.write("x")
os.utime(old_log, (1000, 1000))
os.utime(new_log, (2000, 2000))
check("copy: newest log by mtime", jt._latest_log_path() == new_log, jt._latest_log_path())
os.remove(old_log)
os.remove(new_log)
check("copy: empty dir -> None", jt._latest_log_path() is None)
os.rmdir(jt.LOG_DIR)
check("copy: missing dir -> None", jt._latest_log_path() is None)

# main(): 'l' with a log present -> path copied + feedback line; no oob
# (OSC 52 first: the real _copy_to_clipboard, no clipboard tool, fd 1 captured)
_env = {k: os.environ.pop(k) for k in ("DISPLAY", "WAYLAND_DISPLAY") if k in os.environ}
try:
    fd1 = os.dup(1)
    cap = tempfile.NamedTemporaryFile(delete=False)
    os.dup2(cap.fileno(), 1)
    try:
        ok, how = _real_copy("/tmp/some-log.log")
    finally:
        os.dup2(fd1, 1)
        os.close(fd1)
        cap.close()
    import base64
    out = open(cap.name, "rb").read().decode("ascii")
    exp = "\033]52;c;" + base64.b64encode(b"/tmp/some-log.log").decode("ascii") + "\033\\"
    check("osc52: success + exact sequence", ok and how == "OSC 52" and out == exp, out)
finally:
    os.environ.update(_env)

# fallback: OSC 52 write fails -> local tool used (os.write, shutil.which,
# subprocess.run all stubbed)
os.environ["DISPLAY"] = "fake"
_sh_which = shutil.which
_sp_run = subprocess.run
_os_write = os.write
shutil.which = lambda name: "/bin/" + name   # pretend the tools are installed
subprocess.run = lambda cmd, **k: type("R", (), {"returncode": 0})()
os.write = lambda fd, b: (_ for _ in ()).throw(OSError(5, "write refused"))
try:
    ok, how = _real_copy("/tmp/some-log.log")
    check("copy: OSC52 fail falls back to xclip", ok and how == "xclip", (ok, how))
finally:
    os.write = _os_write
    shutil.which = _sh_which
    subprocess.run = _sp_run
    del os.environ["DISPLAY"]

# main(): 'l' with a log present -> path copied + feedback line; no oob
os.makedirs(jt.LOG_DIR, exist_ok=True)
with open(new_log, "w") as f:
    f.write("x")
os.utime(new_log, (2000, 2000))
json.dump({"selected": "a", "a": {"desc": "d", "cmd": "echo a"}},
          open(jt.COMMANDS_FILE, "w"))
CLIP.clear()
scrC = FakeScr(24, 80, K("l", "q"))
jt.main(scrC)
check("copy: path sent to clipboard", CLIP == [new_log], CLIP)
gridC = "\n".join("".join(r) for r in scrC.grid)
# the full path is longer than one screen row, so only the prefix survives in
# the grid; the clipboard (CLIP) carries the full path (asserted above)
check("copy: feedback logged", "[copy] xclip:" in gridC, gridC[-200:])
check("copy: no oob", scrC.ooobad == 0, scrC.ooobad)
os.remove(new_log)
os.rmdir(jt.LOG_DIR)
# main(): 'l' with no logs -> error line
json.dump({"selected": "a", "a": {"desc": "d", "cmd": "echo a"}},
          open(jt.COMMANDS_FILE, "w"))
scrC2 = FakeScr(24, 80, K("l", "q"))
jt.main(scrC2)
gridC2 = "\n".join("".join(r) for r in scrC2.grid)
check("copy: no-logs error", f"[err] no log files in {jt.LOG_DIR}" in gridC2, gridC2[-300:])
check("copy: no-logs no oob", scrC2.ooobad == 0, scrC2.ooobad)

# ---------- narrow screen smoke ----------
json.dump({"selected": "a", "a": {"desc": "d", "cmd": "echo a"},
           "b": {"desc": "second", "cmd": "echo b"}}, open(jt.COMMANDS_FILE, "w"))
scr_n = FakeScr(12, 40, K("dnq"))  # delete+cancel, quit
jt.main(scr_n)
check("main: narrow screen ok", scr_n.ooobad == 0, scr_n.ooobad)

# ---------- tiny screen smoke (must not crash) ----------
scr3 = FakeScr(8, 20, K("q"))
try:
    jt.main(scr3)
    check("main: tiny screen ok", scr3.ooobad == 0, scr3.ooobad)
except Exception as e:
    check("main: tiny screen ok", False, repr(e))

print(f"\n{len(passed)}/{len(passed) + len(failed)} passed")
if failed:
    print("FAILED:", failed)
sys.exit(1 if failed else 0)
