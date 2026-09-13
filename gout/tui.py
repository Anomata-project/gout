"""The terminal ui: prompt, timeline, panels, cheat sheet, parameter sheet."""
from __future__ import annotations

import contextlib
import io
import math
import os
import re
import shlex
import sys
import textwrap

from .core import __version__, fmt_ms, fmt_pan, GoutError, is_master, MASTER_N
from .model import audible, timeline
from .fx import effect, effects, GUTTER, resolve
from .settings import MASTER_DEFAULTS, master_track, setting
from .render import LABEL_W, render_cheat, render_panel, render_timeline
from .helptext import help_text
from .commands import save_as, slot_of
from .cli import aliases, run


# arrow and paging sequences as curses key names, for terminals that send the plain form
ESCAPE_KEYS = {"[A": "KEY_UP", "OA": "KEY_UP", "[B": "KEY_DOWN", "OB": "KEY_DOWN",
               "[C": "KEY_RIGHT", "OC": "KEY_RIGHT", "[D": "KEY_LEFT", "OD": "KEY_LEFT",
               "[5~": "KEY_PPAGE", "[6~": "KEY_NPAGE", "[Z": "KEY_BTAB"}


class Tui:
    """Left: a prompt with a log, like the terminal. Right: the tracks, like a DAW."""

    def __init__(self, project: Project, scr):
        self.project, self.scr = project, scr
        self.log: list[str] = [f"gout {__version__}  {project.root}",
                               "ctrl-u shows/hides the timeline, ctrl-k the cheat sheet, tab flips its pages,"
                               " ctrl-e opens the parameter sheet"]
        self.input = ""
        self.history: list[str] = []
        self.hist_i: int | None = None
        self.mode = "prompt"  # or "sheet": the parameter table
        self.sheet_rows: list[dict] = []
        self.sheet_cur = 0
        self.sheet_top = 0
        self.edits: dict[str, str] = {}
        self.sheet_errors: dict[str, str] = {}
        self.sheet_status = ""
        self.saveas_name: str | None = None  # the "save as:" field in the sheet while it is open
        self.panel_track: int | None = None  # the track (0: master) whose effect the panel shows
        self.panel_kind = "eq"               # which effect: the one last touched
        self.show_panel = True
        self.show_timeline = (project.get("ui_timeline") or "on") != "off"
        self.show_cheat = (project.get("ui_cheat") or "on") != "off"
        split = project.get("ui_split") or "40"
        self.split = int(split) if split.isdigit() else 40  # left pane, percent of the width
        self.scroll = 0
        self.cheat_scroll = 0
        self.sheet_h = 10
        self.sheet_len = 0
        self.busy = False
        self.running = True

    # ---- drawing

    def put(self, y: int, x: int, text: str, attr: int = 0, maxw: int | None = None) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        if maxw is not None:
            text = text[:maxw]
        text = text[:w - x]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass  # the bottom-right cell always complains

    def layout(self) -> tuple[int, int, int, int | None, int]:
        h, w = self.scr.getmaxyx()
        if (self.show_timeline or self.show_cheat) and w >= 60:
            left = max(30, min(w - 30, w * max(20, min(80, self.split)) // 100))
            return h, w, left, left + 1, w - left - 1
        return h, w, w, None, 0

    def draw(self) -> None:
        import curses
        scr = self.scr
        scr.erase()
        if self.mode == "sheet":
            self.draw_sheet()
            return
        h, w, left_w, right_x, right_w = self.layout()
        p = self.project
        tracks = p.tracks()
        title = (f" gout {p.get('name')}  {p.rate} Hz  {len(tracks)} track{'' if len(tracks) == 1 else 's'}"
                 f"  autorender {'on' if p.autorender else 'off'}").ljust(left_w)
        if self.scroll:
            tag = " ↑ scrolled, pgdn "
            title = title[:max(0, left_w - len(tag))] + tag
        self.put(0, 0, title, curses.A_REVERSE)

        wrapped: list[str] = []
        for line in self.log:
            wrapped.extend(textwrap.wrap(line, max(10, left_w - 1), subsequent_indent="   ",
                                         drop_whitespace=False, replace_whitespace=False) or [""])
        avail = max(0, h - 2)
        self.scroll = max(0, min(self.scroll, max(0, len(wrapped) - avail)))
        if self.scroll == 0 and len(wrapped) <= avail:  # like a terminal: the prompt sits under the output
            visible, prompt_y = wrapped, 1 + len(wrapped)
        else:  # the screen is full (or scrolled back): the prompt stays on the last row
            end = len(wrapped) - self.scroll
            visible, prompt_y = wrapped[max(0, end - avail):end], h - 1
        for i, line in enumerate(visible):
            attr = curses.A_BOLD if line.startswith("> ") else (curses.A_DIM if line.startswith("error") else 0)
            self.put(1 + i, 0, line, attr, left_w - 1)

        prompt = "… " if self.busy else "> "
        room = max(1, left_w - len(prompt) - 1)
        shown = self.input[-room:]
        self.put(prompt_y, 0, prompt + shown, curses.A_DIM if self.busy else curses.A_BOLD)

        if right_x is not None:
            for y in range(h):
                self.put(y, right_x - 1, "│", curses.A_DIM)
            top = 0
            if self.show_timeline:
                self.put(0, right_x, " timeline".ljust(right_w), curses.A_REVERSE)
                rows = render_timeline(p, right_w, styled=True)
                room = max(3, h - 2 - 6) if self.show_cheat else max(3, h - 1)  # sheet keeps six lines
                if self.show_panel and self.panel_track is not None:
                    room = max(3, room - (10 if h >= 32 else 8))
                if len(rows) > room:
                    heads = [r for r in rows if r[2] in ("axis", "ruler")]
                    tail = [r for r in rows if r[2] in ("master", "note") and r not in heads]
                    tracks = [r for r in rows if r[2] == "track"]
                    keep = max(1, room - len(heads) - len(tail) - 1)
                    rows = heads + tracks[:keep] + [("", f"+{len(tracks) - keep} more tracks — ls", "note", "")] + tail
                for y, (label, cells, kind, classes) in enumerate(rows, 1):
                    if y >= h:
                        break
                    self.put(y, right_x, label, curses.A_DIM if kind in ("axis", "ruler", "note") else 0)
                    self.draw_cells(y, right_x + LABEL_W + 1, cells, kind, classes)
                top = 1 + len(rows)

        if right_x is not None and self.show_panel and self.panel_track is not None:
            track = master_track(p) if self.panel_track == MASTER_N else \
                next((t for t in tracks if t["n"] == self.panel_track), None)
            if track is None:
                self.panel_track = None
            else:
                height = 8 if h >= 32 else 6
                rows = render_panel(p, track, right_w - 1, height, self.panel_kind)
                who = "master" if self.panel_track == MASTER_N else f"{track['n']} {track['name']}"
                self.put(top, right_x, (f" {who}  " + rows[0][0])[:right_w - 15].ljust(right_w - 15)
                         + "  ctrl-g hides", curses.A_REVERSE)
                for i, (text, classes, kind) in enumerate(rows[1:], 1):
                    if top + i >= h:
                        break
                    self.put(top + i, right_x + 1, text[:GUTTER], curses.A_DIM)
                    self.draw_cells(top + i, right_x + 1 + GUTTER, text[GUTTER:], kind, classes[GUTTER:])
                top += len(rows)

        if right_x is not None and self.show_cheat:
            sheet = render_cheat(right_w - 1)
            self.sheet_h = max(1, h - top - 1)
            self.sheet_len = len(sheet)
            self.cheat_scroll = max(0, min(self.cheat_scroll, max(0, len(sheet) - self.sheet_h)))
            pages = max(1, math.ceil(len(sheet) / self.sheet_h))
            page = min(pages, math.ceil((self.cheat_scroll + self.sheet_h) / self.sheet_h))
            self.put(top, right_x, f" cheat sheet  {page}/{pages}  tab".ljust(right_w), curses.A_REVERSE)
            for i, line in enumerate(sheet[self.cheat_scroll:self.cheat_scroll + self.sheet_h]):
                header = line[:1].isupper() and not line.startswith(" ")
                self.put(top + 1 + i, right_x + 1, line, curses.A_BOLD if header else 0, right_w - 1)
        try:
            scr.move(prompt_y, min(len(prompt) + len(shown), w - 1))
        except curses.error:
            pass
        scr.refresh()

    def draw_cells(self, y: int, x: int, cells: str, kind: str, classes: str = "") -> None:
        import curses
        if kind in ("note", "axis", "ruler"):
            self.put(y, x, cells, curses.A_DIM)
            return
        attrs = {"a": curses.A_BOLD, "s": curses.A_DIM, "t": curses.A_DIM, "z": curses.A_DIM, "m": curses.A_BOLD,
                 "x": curses.A_DIM}
        classes = classes.ljust(len(cells))
        i = 0
        while i < len(cells):
            j = i
            while j < len(cells) and classes[j] == classes[i]:
                j += 1
            self.put(y, x + i, cells[i:j], attrs.get(classes[i], 0))
            i = j

    # ---- input

    def loop(self) -> None:
        import curses
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        while self.running:
            self.draw()
            try:
                key = self.scr.get_wch()
            except KeyboardInterrupt:
                break
            except curses.error:
                continue
            self.handle(key)

    def handle(self, key) -> None:
        import curses
        if key == curses.KEY_RESIZE:
            return
        if self.mode == "sheet":
            self.handle_sheet(key)
            return
        if key == "\x05":  # ctrl-e
            self.sheet_open()
        elif key == "\x07":  # ctrl-g: the effect panel
            self.toggle_panel()
        elif key == "\x15":  # ctrl-u
            self.toggle("timeline")
        elif key == "\x0b":  # ctrl-k
            self.toggle("cheat")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.submit()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.input = self.input[:-1]
        elif key == "\x04":  # ctrl-d on an empty line leaves
            if not self.input:
                self.running = False
        elif key == "\x0c":  # ctrl-l
            self.log.clear()
        elif key == "\x17":  # ctrl-w
            self.input = self.input.rstrip()
            self.input = self.input[:self.input.rfind(" ") + 1] if " " in self.input else ""
        elif key == "\x1b":
            seq = self.read_escape()
            arrow = re.fullmatch(r"\[1;([235])([CD])", seq)  # shift/alt/ctrl + right/left
            if arrow:
                self.resize(5 if arrow.group(2) == "C" else -5)
            elif seq in ESCAPE_KEYS:  # a terminal that did not follow curses into application mode
                self.handle(getattr(curses, ESCAPE_KEYS[seq]))
            elif not seq:
                self.input = ""
        elif key in (curses.KEY_SLEFT, curses.KEY_SRIGHT):
            self.resize(5 if key == curses.KEY_SRIGHT else -5)
        elif isinstance(key, int) and self.keyname(key)[:4] in (b"kLFT", b"kRIT"):  # ctrl/alt + arrows
            self.resize(5 if self.keyname(key)[:4] == b"kRIT" else -5)
        elif key == curses.KEY_UP:
            if self.history:
                self.hist_i = len(self.history) - 1 if self.hist_i is None else max(0, self.hist_i - 1)
                self.input = self.history[self.hist_i]
        elif key == curses.KEY_DOWN:
            if self.hist_i is not None:
                self.hist_i += 1
                if self.hist_i >= len(self.history):
                    self.hist_i, self.input = None, ""
                else:
                    self.input = self.history[self.hist_i]
        elif key == curses.KEY_PPAGE:
            self.scroll += 10
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
        elif key in ("\t", curses.KEY_BTAB, "\x0e", "\x10"):  # tab, shift-tab, ctrl-n, ctrl-p
            if not self.show_cheat:
                self.toggle("cheat")
                return
            last = max(0, self.sheet_len - self.sheet_h)
            step = self.sheet_h if key in ("\t", curses.KEY_BTAB) else 1
            if key in ("\t", "\x0e"):
                at_end = self.cheat_scroll >= last
                self.cheat_scroll = 0 if at_end and key == "\t" else min(last, self.cheat_scroll + step)
            else:
                at_top = self.cheat_scroll <= 0
                self.cheat_scroll = last if at_top and key == curses.KEY_BTAB else max(0, self.cheat_scroll - step)
        elif isinstance(key, str) and key.isprintable():
            self.input += key
            self.scroll = 0

    # ---- parameter sheet: name | value | new value, ctrl-s applies

    def sheet_build(self) -> list[dict]:
        """Every parameter in the database as a row; edits map to ordinary commands."""
        p = self.project
        rows: list[dict] = []

        def head(text: str) -> None:
            rows.append({"id": None, "head": True, "name": text, "value": "", "hint": "", "cmd": None})

        def row(rid: str, name: str, value: str, cmd, hint: str = "") -> None:
            rows.append({"id": rid, "head": False, "name": name, "value": value, "hint": hint, "cmd": cmd})

        hints = {
            "rate": "Hz", "autorender": "on | off",
            "lufs": "-14 | -16 | -23 | off", "ceiling": "dBTP", "gain": "dB",
            "fadein": "500ms", "fadeout": "3s", "head": "500ms", "tail": "2s",
            "bits": "32f | 24 | 16", "mp3": "320k | 192k | v0", "bpm": "120",
        }

        def fx_rows(spec: str, key: str, items: list[dict]) -> None:
            """A row per effect in the chain (edit: settings, on, off, rm, move N) and one to add."""
            for pos, item in enumerate(items, 1):
                eff = effect(item["kind"])
                value = item["params"] or (eff.empty if eff else "")
                value += ("  (off)" if not item["on"] else "") + ("" if eff else "  (not installed)")
                hint = (eff.hint if eff else "not installed") + " | on | off | rm | move N"
                row(f"fx#{item['id']}", f"{pos} {item['kind']}", value,
                    lambda v, s=spec, fid=item["id"]: ["fx", s, f"#{fid}", *v.split()], hint)
            row(f"{key}:+fx", "+ effect", "", lambda v, s=spec: ["fx", s, "add", *v.split()],
                "KIND [SETTINGS]: " + " | ".join(effects()))
        head(f"project  {p.get('name')}")
        row("set:rate", "rate", str(p.rate), lambda v: ["set", "rate", v], hints["rate"])
        row("set:autorender", "autorender", "on" if p.autorender else "off",
            lambda v: ["set", "autorender", v], hints["autorender"])
        for key in MASTER_DEFAULTS:
            value = setting(p, key)
            if key in ("fadein", "fadeout", "head", "tail") and value != "0":
                value = fmt_ms(int(value))
            row(f"set:{key}", key, value, (lambda k: lambda v: ["set", k, v])(key), hints.get(key, "text"))
        head("master effects  (in order, before master gain and fades)")
        fx_rows("master", "master", master_track(p)["fx"])
        for t in p.tracks():
            n = str(t["n"])
            a, b = audible(t)
            start, _ = timeline(t)
            head(f"track {n}  {t['name']}  {t['kind']}  {fmt_ms(t['length_ms'])}")
            row(f"t{n}:at", "at", fmt_ms(start), lambda v, n=n: ["move", n, "=" + v], "timeline start: 1:30, 90s, -2s")
            row(f"t{n}:in", "in", fmt_ms(a), lambda v, n=n: ["trim", n, "-st", v], "soft trim in, file time")
            row(f"t{n}:out", "out", fmt_ms(b), lambda v, n=n: ["trim", n, "-et", v], "soft trim out, file time")
            row(f"t{n}:gain", "gain", f"{t['gain_db']:g}", lambda v, n=n: ["gain", n, v], "dB")
            row(f"t{n}:pan", "pan", fmt_pan(t["pan"]), lambda v, n=n: ["pan", n, v], "L30 | C | R30")
            row(f"t{n}:mute", "mute", "on" if t["mute"] else "off", lambda v, n=n: ["mute", n, v], "on | off")
            row(f"t{n}:solo", "solo", "on" if t["solo"] else "off", lambda v, n=n: ["solo", n, v], "on | off")
            fx_rows(n, f"t{n}", t["fx"])
        return rows

    def sheet_open(self) -> None:
        self.mode = "sheet"
        self.sheet_rows = self.sheet_build()
        if not self.sheet_rows[self.sheet_cur]["id"] if self.sheet_cur < len(self.sheet_rows) else True:
            self.sheet_cur = next((i for i, r in enumerate(self.sheet_rows) if r["id"]), 0)
        pending = len(self.edits)
        self.sheet_status = f"{pending} unapplied change{'s' if pending != 1 else ''} kept from last time" if pending else ""

    def sheet_close(self) -> None:
        self.mode = "prompt"
        self.saveas_name = None

    def save_as(self, name: str) -> None:
        """Copy the project and carry on in the copy, the way a DAW's Save As does."""
        try:
            copy = save_as(self.project, name)
        except GoutError as exc:
            self.log.append(f"error: {exc}")
            self.sheet_status = str(exc)
            return
        self.project = copy
        self.edits.clear()
        self.sheet_errors.clear()
        self.log.append(f"saved as {copy.root} — you are now working in the copy; the original is untouched")
        self.sheet_status = f"now in {copy.root.name}"

    def sheet_move(self, delta: int) -> None:
        rows = self.sheet_rows
        i = self.sheet_cur
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            j = i + step
            while 0 <= j < len(rows) and not rows[j]["id"]:
                j += step
            if not 0 <= j < len(rows):
                break
            i = j
        self.sheet_cur = i

    def draw_sheet(self) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        rows = self.sheet_rows = self.sheet_build()
        if not (0 <= self.sheet_cur < len(rows)) or not rows[self.sheet_cur]["id"]:
            self.sheet_cur = next((i for i, r in enumerate(rows) if r["id"]), 0)
        pending = len(self.edits)
        title = (f" sheet  {pending} change{'s' if pending != 1 else ''}   ctrl-s apply   ctrl-x apply and close"
                 f"   esc close   ↑ ↓ rows")
        self.put(0, 0, title.ljust(w), curses.A_REVERSE)
        name_w = 12
        val_w = max(14, min(28, (w - name_w - 6) // 3))
        new_x = 2 + name_w + 1 + val_w + 1
        self.put(1, 0, f"  {'name':<{name_w}} {'value':<{val_w}} new value", curses.A_DIM)
        avail = max(1, h - 3)
        if self.sheet_cur < self.sheet_top:
            self.sheet_top = self.sheet_cur
        if self.sheet_cur >= self.sheet_top + avail:
            self.sheet_top = self.sheet_cur - avail + 1
        self.sheet_top = max(0, min(self.sheet_top, max(0, len(rows) - avail)))
        cursor = (h - 1, 0)
        for i, r in enumerate(rows[self.sheet_top:self.sheet_top + avail]):
            y = 2 + i
            if r["head"]:
                self.put(y, 0, r["name"], curses.A_BOLD)
                continue
            current = self.sheet_top + i == self.sheet_cur
            edit = self.edits.get(r["id"])
            mark = "!" if r["id"] in self.sheet_errors else ("*" if edit is not None else " ")
            self.put(y, 0, f"{mark} {r['name']:<{name_w}} {r['value'][:val_w]:<{val_w}} ".ljust(new_x),
                     curses.A_REVERSE if current else 0)
            if edit:
                self.put(y, new_x, edit, curses.A_BOLD | (curses.A_REVERSE if current else 0))
            else:
                self.put(y, new_x, r["hint"], curses.A_DIM)
            if current:
                cursor = (y, min(w - 1, new_x + len(edit or "")))
        if self.saveas_name is not None:
            status = f"save as: {self.saveas_name}   (enter copies the whole project there, esc cancels)"
            cursor = (h - 1, min(w - 1, 9 + len(self.saveas_name)))
            self.put(h - 1, 0, status[:w - 1], curses.A_BOLD)
        else:
            status = self.sheet_errors.get(rows[self.sheet_cur]["id"] or "", "") or self.sheet_status \
                or "type a new value on the highlighted row; enter or ↓ for the next"
            self.put(h - 1, 0, status[:w - 1], curses.A_DIM if not self.sheet_errors else 0)
        try:
            self.scr.move(*cursor)
        except curses.error:
            pass
        self.scr.refresh()

    def sheet_apply(self, close: bool) -> None:
        rows = self.sheet_rows
        done = failed = 0
        buf = io.StringIO()
        for r in rows:
            if not r["id"] or r["id"] not in self.edits:
                continue
            argv = r["cmd"](self.edits[r["id"]]) + ["-N"]
            self.log.append("> " + " ".join(argv))
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(argv, self.project)
                self.edits.pop(r["id"], None)
                self.sheet_errors.pop(r["id"], None)
                done += 1
            except GoutError as exc:
                self.sheet_errors[r["id"]] = str(exc).splitlines()[0]
                buf.write(f"error: {exc}\n")
                failed += 1
        self.log.extend(buf.getvalue().rstrip("\n").splitlines())
        if done and self.project.autorender:
            self.sheet_status = "rendering…"
            self.draw()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(["mix"], self.project)
            except GoutError as exc:
                buf.write(f"error: {exc}\n")
            self.log.extend(buf.getvalue().rstrip("\n").splitlines())
        self.sheet_status = f"applied {done}" + (f", {failed} failed — see the ! rows" if failed else "")
        if close and not failed:
            self.sheet_close()

    def handle_sheet(self, key) -> None:
        import curses
        rows = self.sheet_rows
        rid = rows[self.sheet_cur]["id"] if rows and 0 <= self.sheet_cur < len(rows) else None
        if self.saveas_name is not None:  # typing the name for save as
            if key in ("\n", "\r", curses.KEY_ENTER):
                name, self.saveas_name = self.saveas_name.strip(), None
                if name:
                    if self.edits:
                        self.sheet_apply(close=False)
                    if not self.sheet_errors:
                        self.save_as(name)
            elif key == "\x1b" and not self.read_escape():
                self.saveas_name = None
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                self.saveas_name = self.saveas_name[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.saveas_name += key
            return
        if key == "\x1b":
            seq = self.read_escape()
            if re.fullmatch(r"\[(83|115);6u", seq):  # ctrl-shift-s, in terminals that can send it: save as
                self.saveas_name = ""
                self.sheet_status = ""
            elif seq in ESCAPE_KEYS:
                self.handle_sheet(getattr(curses, ESCAPE_KEYS[seq]))
            elif not seq:
                self.sheet_close()
        elif key == "\x13":  # ctrl-s
            self.sheet_apply(close=False)
        elif key == "\x18":  # ctrl-x
            self.sheet_apply(close=True)
        elif key in ("\x05", "\x11"):  # ctrl-e again, ctrl-q
            self.sheet_close()
        elif key == "\x03":
            self.running = False
        elif key == curses.KEY_UP or key == curses.KEY_BTAB:
            self.sheet_move(-1)
        elif key in (curses.KEY_DOWN, "\n", "\r", curses.KEY_ENTER, "\t"):
            self.sheet_move(1)
        elif key == curses.KEY_PPAGE:
            self.sheet_move(-10)
        elif key == curses.KEY_NPAGE:
            self.sheet_move(10)
        elif rid and key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            edit = self.edits.get(rid, "")[:-1]
            if edit:
                self.edits[rid] = edit
            else:
                self.edits.pop(rid, None)
            self.sheet_errors.pop(rid, None)
        elif rid and key == "\x17":  # ctrl-w clears the cell
            self.edits.pop(rid, None)
            self.sheet_errors.pop(rid, None)
        elif rid and isinstance(key, str) and key.isprintable():
            self.edits[rid] = self.edits.get(rid, "") + key
            self.sheet_errors.pop(rid, None)

    @staticmethod
    def keyname(key: int) -> bytes:
        import curses
        try:
            return curses.keyname(key)
        except (curses.error, ValueError):
            return b""

    def read_escape(self) -> str:
        """The rest of an escape sequence curses did not recognise, or '' for a bare Esc."""
        import curses
        seq = ""
        self.scr.nodelay(True)
        try:
            for _ in range(8):
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    break
                if not isinstance(ch, str):
                    break
                seq += ch
                if ch.isalpha() or ch == "~":
                    break
        finally:
            self.scr.nodelay(False)
        return seq

    def resize(self, delta: int, absolute: int | None = None) -> None:
        self.split = max(20, min(80, self.split + delta if absolute is None else absolute))
        self.project.set("ui_split", str(self.split))
        if not (self.show_timeline or self.show_cheat):
            self.toggle("timeline")

    def toggle_panel(self) -> None:
        if self.panel_track is None:
            tracks = self.project.tracks()
            if not tracks:
                self.log.append("no tracks yet, nothing to show an effect for")
                return
            self.panel_track, self.show_panel = tracks[0]["n"], True
            if tracks[0]["fx"]:
                self.panel_kind = tracks[0]["fx"][0]["kind"]
        else:
            self.show_panel = not self.show_panel

    def follow(self, head: str, argv: list[str]) -> None:
        """Point the effect panel at what an effect command just touched."""
        if len(argv) < 2 or argv[1].lower() in ("presets", "preset", "kinds", "effects"):
            return
        try:
            t = master_track(self.project) if is_master(argv[1]) else self.project.track(argv[1])
        except GoutError:
            return
        kind = None
        if head == "fx":
            if len(argv) > 3 and argv[2].lower() == "add":
                eff = resolve(argv[3])
                kind = eff.name if eff else None
            elif len(argv) > 2:
                try:
                    kind = slot_of(t, argv[2])["kind"]
                except GoutError:
                    kind = None
            elif t["fx"]:
                kind = t["fx"][0]["kind"]
        else:
            eff = resolve(head)
            kind = eff.name if eff else None
        if kind:
            self.panel_track, self.panel_kind, self.show_panel = t["n"], kind, True

    def toggle(self, what: str) -> None:
        """Show or hide one section of the right panel; remembered per project."""
        if what == "timeline":
            self.show_timeline = not self.show_timeline
            self.project.set("ui_timeline", "on" if self.show_timeline else "off")
        else:
            self.show_cheat = not self.show_cheat
            self.project.set("ui_cheat", "on" if self.show_cheat else "off")

    def submit(self) -> None:
        line = self.input.strip()
        self.input, self.scroll, self.hist_i = "", 0, None
        if not line:
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
        self.log.append("> " + line)
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            self.log.append(f"error: {exc}")
            return
        head = aliases().get(argv[0], argv[0])
        is_effect = head == "fx" or resolve(head) is not None
        if head in ("q", "quit", "exit"):
            self.running = False
        elif head in ("view", "timeline"):
            self.toggle("timeline")
        elif head == "cheat":
            self.toggle("cheat")
        elif head == "sheet":
            self.sheet_open()
        elif is_effect and head != "fx" and len(argv) == 1:
            kind = resolve(head).name
            if self.panel_track is not None and self.show_panel and self.panel_kind != kind:
                self.panel_kind = kind  # switch the panel to that effect rather than hiding it
            else:
                if self.panel_track is None or not self.show_panel:
                    self.panel_kind = kind
                self.toggle_panel()
        elif head == "clear":
            self.log.clear()
        elif head == "split":
            arg = argv[1] if len(argv) > 1 else ""
            if re.fullmatch(r"[+-]\d+", arg):
                self.resize(int(arg))
            elif arg.isdigit():
                self.resize(0, int(arg))
            elif arg:
                self.log.append("split takes a percentage (split 50) or a step (split +5, split -5)")
            self.log.append(f"split  left pane {self.split}% of the width  (ctrl-← ctrl-→ move it)")
        elif head in ("help", "-h", "--help"):
            full = argv[1:] == ["all"]
            self.log.extend(help_text().rstrip().splitlines() if full else render_cheat(max(40, self.layout()[2] - 2)))
        elif head == "saveas":
            if len(argv) != 2:
                self.log.append("saveas NAME  (a bare name goes next to this project; a path goes where it says)")
            else:
                self.save_as(argv[1])
        elif head in ("ui", "tui", "rebuild", "new"):
            self.log.append(f"{head}: run that from the shell")
        else:
            self.busy = True
            self.draw()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(argv, self.project)
            except GoutError as exc:
                buf.write(f"error: {exc}\n")
            except Exception as exc:  # keep the UI alive whatever happens
                buf.write(f"error: {type(exc).__name__}: {exc}\n")
            finally:
                self.busy = False
            self.log.extend(buf.getvalue().rstrip("\n").splitlines())
            if is_effect:
                self.follow(head, argv)
        del self.log[:-2000]


def run_tui(project: Project) -> None:
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # a bare Esc should not wait a second

    def start(scr) -> None:
        try:  # ctrl-s is XOFF to the terminal driver unless IXON is off; endwin restores the old mode
            import termios
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        Tui(project, scr).loop()

    curses.wrapper(start)
