"""The terminal ui: prompt, timeline, panels, cheat sheet, parameter sheet."""
from __future__ import annotations

import contextlib
import io
import math
import os
import re
import shlex
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from .core import __version__, fmt_ms, fmt_pan, GoutError, is_master, MASTER_N, MASTER_WAV, parse_time
from . import analysis
from .model import audible, timeline
from .fx import effect, effects, GUTTER, resolve
from .settings import MASTER_DEFAULTS, master_track, setting
from .render import CHEAT_HEADINGS, cheat_layout, LABEL_W, panel_head, render_cheat, render_panel, render_timeline
from .helptext import help_text
from .commands import save_as, slot_of
from .cli import aliases, command_table, run
from .lineedit import LineEditor, path_candidates
from .player import Player
from .screens import screen_for_key, screen_named, ScreenContext, screens
from .theme import load_theme, Palette
from .commands import head_seconds, player_for


# arrow, paging and editing sequences as curses key names, for terminals that send the plain form
ESCAPE_KEYS = {"[A": "KEY_UP", "OA": "KEY_UP", "[B": "KEY_DOWN", "OB": "KEY_DOWN",
               "[C": "KEY_RIGHT", "OC": "KEY_RIGHT", "[D": "KEY_LEFT", "OD": "KEY_LEFT",
               "[5~": "KEY_PPAGE", "[6~": "KEY_NPAGE", "[Z": "KEY_BTAB",
               "[H": "KEY_HOME", "OH": "KEY_HOME", "[1~": "KEY_HOME", "[7~": "KEY_HOME",
               "[F": "KEY_END", "OF": "KEY_END", "[4~": "KEY_END", "[8~": "KEY_END", "[3~": "KEY_DC"}

# commands whose first word is a track (or master); the rest take files where they take anything
TRACK_FIRST = {"move", "trim", "rm", "mute", "solo", "gain", "pan", "fx"}
UI_WORDS = ("quit", "clear", "split", "sheet", "view", "help")
HISTORY_FILE = ".gout/ui-history"
IDLE_RENDER_SECONDS = 1.5  # how long nothing must change before the ui renders in the background
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HISTORY_KEEP = 500


def header_line(text: str, hint: str, width: int) -> str:
    """A title bar: text on the left, the key hint against the right edge; the text gives way."""
    if len(hint) + 2 > width:
        return text[:width].ljust(width)
    room = width - len(hint)
    return text[:room - 2].ljust(room) + hint


class Tui:
    """Left: a prompt with a log, like the terminal. Right: the tracks, like a DAW."""

    def __init__(self, project: Project, scr):
        self.project, self.scr = project, scr
        self.log: list[str] = [f"gout {__version__}  {project.root}",
                               "ctrl-u undoes the last change, ctrl-t shows/hides the timeline, ctrl-k the cheat sheet,"
                               " tab flips its pages, ctrl-e opens the parameter sheet"]
        self.theme, theme_problems, self.theme_path = load_theme(project.root)
        self.palette = Palette(self.theme)
        self.player: Player | None = None
        self.playhead_ms = 0  # project time; stays where playback stopped
        self.render_proc: subprocess.Popen | None = None  # a background mix, in its own process
        self.render_state = ""       # the project state that render is making master.wav of
        self.seen_state = ""         # the state at the last look, to notice changes
        self.changed_at = 0.0
        self.given_up_state = ""     # a state a render could not make current (nothing audible)
        self.start_dir = Path.cwd()  # file names complete from where gout was started
        self.line = LineEditor(self.complete_words)
        self.line.history = self.load_history()
        self.log += [f"error: color.json: {problem}" for problem in theme_problems]
        self.mode = "prompt"  # or "sheet": the parameter table, or "screen": an addon's full-screen view
        self.screen = None     # the open Screen
        self.screen_ctx: ScreenContext | None = None
        self.sheet_rows: list[dict] = []
        self.sheet_cur = 0
        self.sheet_top = 0
        self.edits: dict[str, str] = {}
        self.sheet_errors: dict[str, str] = {}
        self.sheet_status = ""
        self.saveas_name: str | None = None  # the "save as:" field in the sheet while it is open
        self.panel_track: int | None = None  # the track (0: master) whose effect the panel shows
        self.panel_kind = "eq"               # which effect: the one last touched
        self.show_panel = (project.get("ui_fx_pictures") or "on") != "off"  # the pictures; the name line always shows
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

    # ---- the command line

    @property
    def input(self) -> str:
        return self.line.text

    @input.setter
    def input(self, text: str) -> None:
        self.line.set(text)

    @property
    def history(self) -> list[str]:
        return self.line.history

    def load_history(self) -> list[str]:
        try:
            lines = (self.project.root / HISTORY_FILE).read_text().splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()][-HISTORY_KEEP:]

    def save_history(self) -> None:
        path = self.project.root / HISTORY_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(self.line.history[-HISTORY_KEEP:]) + "\n")
        except OSError:
            pass

    def complete_words(self, before: list[str], value: str) -> list[str]:
        """What the word under the cursor can become: a command, a track, a preset, or a file."""
        if not before:
            names = sorted(set(command_table()) | set(UI_WORDS) | set(screens()))
            return [n for n in names if n.startswith(value)]
        head = aliases().get(before[0], before[0])
        eff = resolve(head)
        if len(before) == 1 and (head in TRACK_FIRST or eff is not None):
            names = [t["name"] for t in self.project.tracks()] + ["master"] * (eff is not None or head == "fx")
            names += ["all"] * (head in ("mute", "solo"))
            names += ["presets"] * (eff is not None) + ["kinds"] * (head == "fx")
            matches = [n for n in names if n.startswith(value)]
            if matches or value[:1] not in ("/", ".", "~"):
                return matches
        if len(before) == 2 and eff is not None and head == eff.name:
            words = list(eff.presets) + ["on", "off", "clear"]
            return [w for w in words if w.startswith(value)]
        if len(before) >= 2 and head == "fx" and before[-1].lower() == "add":
            return [name for name in effects() if name.startswith(value)]
        if head in TRACK_FIRST or eff is not None:
            return []
        return path_candidates(self.start_dir, value)

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
        if self.mode == "screen":
            self.draw_screen()
            return
        h, w, left_w, right_x, right_w = self.layout()
        p = self.project
        tracks = p.tracks()
        title = (f" gout {p.get('name')}  {p.rate} Hz  {len(tracks)} track{'' if len(tracks) == 1 else 's'}"
                 f"  autorender {p.render_mode}")
        hidden = [f"{key} {what}" for key, what, shown in (("ctrl-t", "timeline", self.show_timeline),
                                                           ("ctrl-k", "cheat sheet", self.show_cheat)) if not shown]
        hint = " ↑ scrolled, pgdn " if self.scroll else ("  ".join(hidden) + " " if hidden else "")
        self.put(0, 0, header_line(title, hint, left_w), self.palette.attr("header"))

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
            attr = (self.palette.attr("command_echo") if line.startswith("> ")
                    else (self.palette.attr("error") if line.startswith("error") else 0))
            self.put(1 + i, 0, line, attr, left_w - 1)

        prompt = "… " if self.busy else "> "
        room = max(1, left_w - len(prompt) - 1)
        text, cursor = self.line.text, self.line.cursor
        first = max(0, cursor - room + 1)  # scroll sideways to keep the cursor in view
        shown = text[first:first + room]
        self.put(prompt_y, 0, prompt, self.palette.attr("suggestion") if self.busy else self.palette.attr("prompt"))
        self.put(prompt_y, len(prompt), shown, curses.A_DIM if self.busy else curses.A_BOLD)
        if not self.busy and cursor == len(text):
            ghost = self.line.suggestion()[:max(0, room - len(shown))]
            if ghost:
                self.put(prompt_y, len(prompt) + len(shown), ghost, self.palette.attr("suggestion"))
        cursor_x = len(prompt) + cursor - first

        if right_x is not None:
            for y in range(h):
                self.put(y, right_x - 1, "│", self.palette.attr("gap_line"))
            top = 0
            if self.show_timeline:
                where = self.play_position_ms()
                state = (f"  ▶ {fmt_ms(where)}{'  live' if self.player.live else ''}  space stops" if self.player
                         else (f"  ■ {fmt_ms(where)}  space plays" if where else "  space plays"))
                if self.render_proc is not None:
                    state += "   rendering master.wav…"
                self.put(0, right_x, header_line(" timeline" + state, "ctrl-t hides ", right_w), self.palette.attr("header"))
                room = max(3, h - 2 - 6) if self.show_cheat else max(3, h - 1)  # the cheat sheet keeps six lines
                if self.panel_track is not None:
                    room = max(3, room - ((10 if h >= 32 else 8) if self.show_panel else 1))
                rows = render_timeline(p, right_w, styled=True, playhead_ms=where if (self.player or where) else None,
                                       max_rows=room, theme=self.theme)
                for y, (label, cells, kind, classes, role) in enumerate(rows, 1):
                    if y >= h:
                        break
                    self.put(y, right_x, label, self.palette.attr(role) if role else 0)
                    self.draw_cells(y, right_x + LABEL_W + 1, cells, kind, classes)
                top = 1 + len(rows)

        if right_x is not None and self.panel_track is not None:
            track = master_track(p) if self.panel_track == MASTER_N else \
                next((t for t in tracks if t["n"] == self.panel_track), None)
            if track is None:
                self.panel_track = None
            else:
                height = 8 if h >= 32 else 6
                rows = (render_panel(p, track, right_w - 1, height, self.panel_kind) if self.show_panel
                        else [(panel_head(track, self.panel_kind), "", "head")])
                who = "master" if self.panel_track == MASTER_N else f"{track['n']} {track['name']}"
                hint = "ctrl-g hides " if self.show_panel else "ctrl-g shows "
                self.put(top, right_x, header_line(f" {who}  " + rows[0][0], hint, right_w), self.palette.attr("header"))
                for i, (text, classes, kind) in enumerate(rows[1:], 1):
                    if top + i >= h:
                        break
                    self.put(top + i, right_x + 1, text[:GUTTER], self.palette.attr("ruler_labels"))
                    self.draw_cells(top + i, right_x + 1 + GUTTER, text[GUTTER:], kind, classes[GUTTER:])
                top += len(rows)

        if right_x is not None and self.show_cheat:
            sheet, second = cheat_layout(right_w - 1)
            self.sheet_h = max(1, h - top - 1)
            self.sheet_len = len(sheet)
            self.cheat_scroll = max(0, min(self.cheat_scroll, max(0, len(sheet) - self.sheet_h)))
            pages = max(1, math.ceil(len(sheet) / self.sheet_h))
            page = min(pages, math.ceil((self.cheat_scroll + self.sheet_h) / self.sheet_h))
            self.put(top, right_x, header_line(f" cheat sheet  {page}/{pages}  tab pages", "ctrl-k hides ", right_w),
                     self.palette.attr("header"))
            for i, line in enumerate(sheet[self.cheat_scroll:self.cheat_scroll + self.sheet_h]):
                self.put(top + 1 + i, right_x + 1, line, 0, right_w - 1)
                for at in (0, second):  # section names start a column
                    if at is None or at >= len(line) or (at and line[at - 1] != " "):
                        continue
                    heading = line[at:].split(" ", 1)[0]
                    if heading in CHEAT_HEADINGS and at < right_w - 1:
                        self.put(top + 1 + i, right_x + 1 + at, heading, self.palette.attr("cheat_heading"),
                                 right_w - 1 - at)
        try:
            scr.move(prompt_y, min(cursor_x, w - 1))
        except curses.error:
            pass
        scr.refresh()

    def draw_cells(self, y: int, x: int, cells: str, kind: str, classes: str = "") -> None:
        import curses
        if kind == "note":
            self.put(y, x, cells, self.palette.attr("ruler_labels"))
            return
        classes = classes.ljust(len(cells))
        i = 0
        while i < len(cells):
            j = i
            while j < len(cells) and classes[j] == classes[i]:
                j += 1
            self.put(y, x + i, cells[i:j], self.class_attr(classes[i]))
            i = j

    CLASS_ROLES = {"m": "master_wave", "s": "muted_wave", "t": "trimmed_wave", "c": "center_line", "r": "ruler",
                   "l": "ruler_labels", "p": "playhead", "g": "gap_line", "a": "effect_curve", "z": "center_line",
                   "x": "trimmed_wave"}

    def class_attr(self, cls: str) -> int:
        """Attributes for a cell class: theme roles, or a track's palette colour for digits."""
        if cls in self.CLASS_ROLES:
            return self.palette.attr(self.CLASS_ROLES[cls])
        if cls >= "0" and cls < "a" and cls != " ":
            return self.palette.track(ord(cls) - 0x30)
        return 0

    # ---- input

    def loop(self) -> None:
        import curses
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.palette.enable()
        try:
            while self.running:
                self.check_player()
                began = time.monotonic()
                self.draw()
                spent_ms = (time.monotonic() - began) * 1000
                self.background_render()
                waiting = self.render_proc is not None or self.project.render_mode == "idle"
                if self.mode == "screen":  # frames at the screen's rate while the song plays
                    self.scr.timeout(max(5, round(1000 / max(1, self.screen.fps) - spent_ms)) if self.player else 250)
                else:
                    self.scr.timeout(100 if self.player else (250 if waiting else -1))  # playhead, renders
                try:
                    key = self.scr.get_wch()
                except KeyboardInterrupt:
                    break
                except curses.error:
                    continue
                self.handle(key)
        finally:
            self.stop_playing(keep=False)
            self.cancel_render()

    # ---- playback

    def play_position_ms(self) -> int:
        if self.player is None:
            return self.playhead_ms
        return round((self.player.position() - head_seconds(self.project)) * 1000)

    def check_player(self) -> None:
        if self.player is not None and not self.player.running():
            self.player = None
            self.playhead_ms = 0  # played to the end: back to the start

    def start_playing(self, from_ms: int | None = None) -> None:
        """Play from the playhead (or from_ms): master.wav when it matches the project, the project
        streamed live when it does not, so a change is heard at once."""
        self.stop_playing(keep=False)
        if from_ms is not None:
            self.playhead_ms = max(0, from_ms)
        p = self.project
        buf = io.StringIO()
        for attempt in (0, 1):
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    player = player_for(p, self.playhead_ms)
                    player.start()
                break
            except GoutError as exc:
                if attempt == 0 and self.playhead_ms and "past the end" in str(exc):
                    self.playhead_ms = 0  # at the end: from the top
                    continue
                self.log.extend(buf.getvalue().rstrip("\n").splitlines())
                self.log.append(f"error: {exc}")
                return
        self.log.extend(buf.getvalue().rstrip("\n").splitlines())
        self.player = player
        how = "live, as the project is now" if player.live else MASTER_WAV
        self.log.append(f"play  {how} from {fmt_ms(self.playhead_ms)}  ({player.backend}; space stops)")

    # ---- rendering in the background

    def background_render(self, now: float | None = None) -> None:
        """With autorender idle: once the project has not changed for a moment and master.wav is
        out of date, render it in a separate process; a change meanwhile cancels that render."""
        now = time.monotonic() if now is None else now
        p = self.project
        if self.render_proc is not None:
            if self.render_proc.poll() is None:
                if p.state_fingerprint() != self.render_state:
                    self.cancel_render()
                    self.log.append("render  the project changed: starting over when it settles")
                    self.seen_state, self.changed_at = "", now
                return
            output, _ = self.render_proc.communicate()
            self.render_proc = None
            self.log.extend(line for line in output.rstrip("\n").splitlines() if line.strip())
            if not p.master_is_current():
                self.given_up_state = self.render_state
            return
        if p.render_mode != "idle" or self.player is not None or self.busy or self.mode != "prompt":
            return
        state = p.state_fingerprint()
        if state != self.seen_state:
            self.seen_state, self.changed_at = state, now
            return
        if now - self.changed_at < IDLE_RENDER_SECONDS or state == self.given_up_state:
            return
        if not p.tracks() or p.master_is_current():
            return
        code = (f"import sys; sys.path.insert(0, {str(PACKAGE_ROOT)!r}); from gout.cli import main; "
                f"sys.exit(main(['-p', {str(p.root)!r}, 'mix']))")
        self.render_proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                            start_new_session=True)
        self.render_state = state

    def cancel_render(self) -> None:
        if self.render_proc is None:
            return
        if self.render_proc.poll() is None:
            try:
                os.killpg(self.render_proc.pid, 15)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.render_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.render_proc.kill()
        if self.render_proc.stdout:
            self.render_proc.stdout.close()
        self.render_proc = None
        for leftover in ("master.raw.part.wav", "master.part.wav"):
            (self.project.root / leftover).unlink(missing_ok=True)

    def stop_playing(self, keep: bool = True) -> None:
        if self.player is None:
            return
        where = self.play_position_ms()
        self.player.stop()
        self.player = None
        if keep:
            self.playhead_ms = max(0, where)
            self.log.append(f"stop  at {fmt_ms(self.playhead_ms)}")

    def seek(self, delta_ms: int) -> None:
        position = max(0, self.play_position_ms() + delta_ms)
        if self.player is not None:
            self.start_playing(position)
        else:
            self.playhead_ms = position

    def run_logged(self, argv: list[str]) -> None:
        self.busy = True
        self.draw()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                run(argv, self.project)
        except GoutError as exc:
            buf.write(f"error: {exc}\n")
        finally:
            self.busy = False
        self.log.extend(buf.getvalue().rstrip("\n").splitlines())

    def handle(self, key) -> None:
        import curses
        if key == curses.KEY_RESIZE:
            return
        if self.mode == "sheet":
            self.handle_sheet(key)
            return
        if self.mode == "screen":
            self.handle_screen(key)
            return
        if screen_for_key(key) is not None:  # a key an addon's screen took: ctrl-space for the fractal
            self.open_screen(screen_for_key(key))
            return
        if key == "\x05":  # ctrl-e
            self.sheet_open()
        elif key == "\x07":  # ctrl-g: the effect panel
            self.toggle_panel()
        elif key == "\x14":  # ctrl-t
            self.toggle("timeline")
        elif key == "\x15":  # ctrl-u: undo the last change, whatever is on the line
            self.scroll = 0
            self.log.append("> undo  (ctrl-u)")
            self.run_command(["undo"], "undo", False)
        elif key == "\x0b":  # ctrl-k
            self.toggle("cheat")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.submit()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.line.backspace()
        elif key == curses.KEY_DC:
            self.line.delete()
        elif key == " " and not self.input:  # space on an empty line: play / stop, as in a DAW
            if self.player:
                self.stop_playing()
            else:
                self.start_playing()
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and not self.input and (self.player or self.playhead_ms):
            self.seek(-5000 if key == curses.KEY_LEFT else 5000)  # empty line: move the playhead 5 s
        elif key == curses.KEY_LEFT:
            self.line.left()
        elif key in (curses.KEY_RIGHT, "\x06"):  # right, ctrl-f: move, or take the suggestion at the end
            self.line.right()
        elif key in (curses.KEY_HOME, "\x01"):  # home, ctrl-a
            self.line.home()
        elif key == curses.KEY_END:
            self.line.end()
        elif key == "\x04":  # ctrl-d: delete under the cursor; on an empty line, leave
            if not self.input:
                self.running = False
            else:
                self.line.delete()
        elif key == "\x0c":  # ctrl-l
            self.log.clear()
        elif key == "\x17":  # ctrl-w
            self.line.delete_word()
        elif key == "\x1b":
            seq = self.read_escape()
            arrow = re.fullmatch(r"\[1;([235])([CD])", seq)  # shift/alt/ctrl + right/left
            if arrow:
                self.resize(5 if arrow.group(2) == "C" else -5)
            elif seq in ESCAPE_KEYS:  # a terminal that did not follow curses into application mode
                self.handle(getattr(curses, ESCAPE_KEYS[seq]))
            elif not seq:
                self.line.clear()
        elif key in (curses.KEY_SLEFT, curses.KEY_SRIGHT):
            self.resize(5 if key == curses.KEY_SRIGHT else -5)
        elif isinstance(key, int) and self.keyname(key)[:4] in (b"kLFT", b"kRIT"):  # ctrl/alt + arrows
            self.resize(5 if self.keyname(key)[:4] == b"kRIT" else -5)
        elif key == curses.KEY_UP:
            self.line.up()
        elif key == curses.KEY_DOWN:
            self.line.down()
        elif key == curses.KEY_PPAGE:
            self.scroll += 10
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
        elif key == "\t" and self.input.strip():  # tab on a line: complete the word under the cursor
            choices = self.line.complete()
            if choices:
                shown = choices[:40]
                self.log.append("  ".join(c.rstrip("/").rsplit("/", 1)[-1] + ("/" if c.endswith("/") else "")
                                          for c in shown) + (f"  … {len(choices) - 40} more" if len(choices) > 40 else ""))
                self.scroll = 0
        elif key in ("\t", curses.KEY_BTAB, "\x0e", "\x10"):  # tab on an empty line, shift-tab, ctrl-n, ctrl-p
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
            self.line.insert(key)
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
            "rate": "Hz", "autorender": "idle | on | off",
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
        row("set:autorender", "autorender", p.render_mode,
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
        self.put(0, 0, title.ljust(w), self.palette.attr("header"))
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
                self.put(y, 0, r["name"], self.palette.attr("cheat_heading"))
                continue
            current = self.sheet_top + i == self.sheet_cur
            edit = self.edits.get(r["id"])
            mark = "!" if r["id"] in self.sheet_errors else ("*" if edit is not None else " ")
            self.put(y, 0, f"{mark} {r['name']:<{name_w}} {r['value'][:val_w]:<{val_w}} ".ljust(new_x),
                     curses.A_REVERSE if current else 0)
            if edit:
                self.put(y, new_x, edit, self.palette.attr("sheet_edit") | (curses.A_REVERSE if current else 0))
            else:
                self.put(y, new_x, r["hint"], self.palette.attr("suggestion"))
            if current:
                cursor = (y, min(w - 1, new_x + len(edit or "")))
        if self.saveas_name is not None:
            status = f"save as: {self.saveas_name}   (enter copies the whole project there, esc cancels)"
            cursor = (h - 1, min(w - 1, 9 + len(self.saveas_name)))
            self.put(h - 1, 0, status[:w - 1], curses.A_BOLD)
        else:
            status = self.sheet_errors.get(rows[self.sheet_cur]["id"] or "", "") or self.sheet_status \
                or "type a new value on the highlighted row; enter or ↓ for the next"
            self.put(h - 1, 0, status[:w - 1], self.palette.attr("error") if self.sheet_errors else self.palette.attr("suggestion"))
        try:
            self.scr.move(*cursor)
        except curses.error:
            pass
        self.scr.refresh()

    def sheet_apply(self, close: bool) -> None:
        rows = self.sheet_rows
        done = failed = 0
        before = self.chain_kinds()
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
        self.settle_panel(before)
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
            self.panel_track = tracks[0]["n"]
            if tracks[0]["fx"]:
                self.panel_kind = tracks[0]["fx"][0]["kind"]
            if self.show_panel:
                return
        self.show_panel = not self.show_panel  # the pictures only: the name line stays
        self.project.set("ui_fx_pictures", "on" if self.show_panel else "off")

    # ---- screens: an addon's full-screen view

    SCREEN_KEY_NAMES = {"KEY_UP": "up", "KEY_DOWN": "down", "KEY_PPAGE": "pgup", "KEY_NPAGE": "pgdn",
                        "KEY_HOME": "home", "KEY_END": "end", "KEY_ENTER": "enter", "KEY_BACKSPACE": "backspace"}

    def open_screen(self, screen) -> None:
        import curses
        tracks = self.project.tracks()
        if not tracks:
            self.log.append(f"{screen.name}: no tracks yet, nothing to play")
            return
        ctx = ScreenContext(self.project)
        ctx.length_ms = max((timeline(t)[1] for t in tracks), default=0)
        ctx.note = "listening to the song…"
        self.screen, self.screen_ctx, self.mode = screen, ctx, "screen"
        plan = analysis.plan(self.project)  # the database here; the files in the background

        def listen() -> None:
            try:
                ctx.features = analysis.compute(plan)
                ctx.note = ""
            except Exception as exc:  # the screen still moves with time
                ctx.note = f"could not listen: {exc}"
        threading.Thread(target=listen, daemon=True).start()
        if screen.play_on_open and self.player is None:
            self.start_playing()
        try:
            curses.curs_set(0)
        except Exception:
            pass

    def close_screen(self) -> None:
        import curses
        self.mode, self.screen, self.screen_ctx = "prompt", None, None
        try:
            curses.curs_set(1)
        except Exception:
            pass

    def draw_screen(self) -> None:
        h, w = self.scr.getmaxyx()
        ctx, screen = self.screen_ctx, self.screen
        ctx.position_ms = float(self.play_position_ms())
        ctx.playing = self.player is not None
        try:
            rows = screen.frame(ctx, w, max(1, h - 1))
        except Exception as exc:  # an addon must never take the ui down with it
            self.log.append(f"error: {screen.name} screen: {type(exc).__name__}: {exc}")
            self.close_screen()
            self.draw()
            return
        for y, (text, classes) in enumerate(rows[:max(0, h - 1)]):
            self.draw_cells(y, 0, text[:w], "screen", classes[:w])
        state = "▶" if ctx.playing else "■"
        left = f" {screen.status(ctx)}   {state} {fmt_ms(round(ctx.position_ms))} / {fmt_ms(ctx.length_ms)}"
        if ctx.note:
            left += f"   {ctx.note}"
        hint = f"space {'stops' if ctx.playing else 'plays'}  ← → 5 s  esc back to gout "
        self.put(h - 1, 0, header_line(left, hint, w), self.palette.attr("header"))

    def handle_screen(self, key) -> None:
        import curses
        screen = self.screen
        if key == "\x1b":
            seq = self.read_escape()
            if not seq:
                self.close_screen()
                return
            if seq not in ESCAPE_KEYS:
                return
            key = getattr(curses, ESCAPE_KEYS[seq])
        if screen_for_key(key) is screen:
            self.close_screen()
        elif key == " ":
            if self.player:
                self.stop_playing()
            else:
                self.start_playing()
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.seek(-5000 if key == curses.KEY_LEFT else 5000)
        elif key == curses.KEY_RESIZE:
            return
        else:
            if isinstance(key, int):
                name = next((n for code, n in self.SCREEN_KEY_NAMES.items() if getattr(curses, code, None) == key), None)
            else:
                name = {"\n": "enter", "\r": "enter", "\t": "tab", "\x7f": "backspace"}.get(key, key)
            if name is None:
                return
            try:
                screen.key_pressed(self.screen_ctx, name)
            except Exception as exc:
                self.log.append(f"error: {screen.name} screen: {type(exc).__name__}: {exc}")
                self.close_screen()

    def chain_kinds(self) -> dict[int, list[str]]:
        """The effect kinds on every track and the master, by track number (master: MASTER_N)."""
        chains = {t["n"]: [i["kind"] for i in t["fx"]] for t in self.project.tracks()}
        chains[MASTER_N] = [i["kind"] for i in master_track(self.project)["fx"]]
        return chains

    def settle_panel(self, before: dict[int, list[str]]) -> None:
        """After a change: when the effect the panel shows was on its track and is gone now (fx
        clear, fx N rm, KIND clear, undo, the sheet), show the first effect left there, or no
        panel line when the chain is empty. A look at an effect the track never had (eq 3 on a
        track without an eq) keeps showing it as none."""
        if self.panel_track is None:
            return
        after = self.chain_kinds()
        if self.panel_track not in after:
            self.panel_track = None
            return
        if self.panel_kind not in before.get(self.panel_track, []) or self.panel_kind in after[self.panel_track]:
            return
        left = [kind for kind in after[self.panel_track] if effect(kind) is not None]
        if left:
            self.panel_kind = left[0]
        else:
            self.panel_track = None

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
        if kind:  # the pictures stay as ctrl-g left them
            self.panel_track, self.panel_kind = t["n"], kind

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
        self.line.clear()
        self.scroll = 0
        if not line:
            return
        self.line.remember(line)
        self.save_history()
        self.log.append("> " + line)
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            argv = None
            for closing in ('"', "'"):  # a quote left open at the end of the line
                try:
                    argv = shlex.split(line + closing)
                    break
                except ValueError:
                    continue
            if argv is None:
                self.log.append(f"error: {exc}")
                return
        head = aliases().get(argv[0], argv[0])
        is_effect = head == "fx" or resolve(head) is not None
        if head in ("q", "quit", "exit"):
            self.stop_playing(keep=False)
            self.cancel_render()
            self.running = False
        elif screen_named(head) is not None and len(argv) == 1:
            self.open_screen(screen_named(head))
        elif head in ("view", "timeline"):
            self.toggle("timeline")
        elif head == "cheat":
            self.toggle("cheat")
        elif head == "sheet":
            self.sheet_open()
        elif head == "play":
            if len(argv) > 1:
                try:
                    from_ms = round(parse_time(argv[1]) * 1000)
                except ValueError as exc:
                    self.log.append(f"error: {exc}")
                    return
                self.start_playing(from_ms)
            else:
                self.start_playing()
        elif head == "stop":
            if self.player:
                self.stop_playing()
            else:
                self.playhead_ms = 0
                self.log.append("stop  playhead back to the start")
        elif is_effect and head != "fx" and len(argv) == 1:
            kind = resolve(head).name
            if self.panel_track is not None and self.panel_kind != kind:
                self.panel_kind = kind  # switch the panel to that effect rather than hiding its pictures
            else:
                if self.panel_track is None:
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
            self.run_command(argv, head, is_effect)
        del self.log[:-2000]

    def run_command(self, argv: list[str], head: str, is_effect: bool) -> None:
        """Run a gout command against the project, its output into the log."""
        self.busy = True
        self.draw()
        before = self.chain_kinds()
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
        self.settle_panel(before)
        del self.log[:-2000]


def run_tui(project: Project) -> None:
    import curses
    import locale
    effects()  # addons load (and report problems) before curses takes the screen
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
