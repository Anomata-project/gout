"""The grid: an instrument track's pattern on the whole screen, as a sheet of cells.

There is no curses in here. Grid holds what is shown and where the cursor is, turns keys into
gout command lines and gives the screen back as rows of (column, text, role). The ui runs the
commands, so an edit in the grid is an ordinary command: one undo step, written to gout.json,
heard at once while the song plays.

The first row is the instrument as a whole: title, description, steps, loop, step and its own
cells, with a + at the end that adds one of the cells it has more of. Under it every feature is
a row and every step a column; the column before the steps holds the row's value for every step,
and a + after the last step adds steps.

It works like a sheet. The arrows go from cell to cell, tab to the next cell and shift-tab to
the one before, row after row. Typing puts a new value in the cell and enter opens the value it
has to change it; enter, tab or an arrow takes what was typed, esc lets it go. Delete empties a
cell, + and - move its value a little, page up and page down a lot.
"""
from __future__ import annotations

from .core import GoutError
from .inst import BASE_SETTINGS, instrument
from .pattern import Feature, HOLD, note_name, Pattern, REST
from .sequencer import context

EMPTY = "·"
HINT = " arrows and tab move   type a value, enter opens the one there   del empties   + - a little, pgup pgdn a lot   space plays   ctrl-u undoes"
MOVES = ("left", "right", "up", "down", "home", "end", "tab", "btab")


class Grid:
    def __init__(self, project, spec: str):
        self.project = project
        self.spec = spec
        self.row, self.col = 1, 1          # row 0 is the first row; in the others column 0 is "all", then the steps
        self.col_first, self.col_steps = 0, 1   # where the cursor was in the first row, and among the steps
        self.edit: str | None = None       # what is being typed into the cell
        self.message = ""                  # the last thing that went wrong, or was done
        self.extra: list[str] = []         # cells added with + in this sitting
        self.choosing: list[Feature] | None = None   # the list + opens
        self.choice = 0
        self.load()

    # ---- what there is

    def load(self) -> None:
        """Read the track again: after every command, the pattern may be another."""
        on_more = hasattr(self, "pattern") and self.row > 0 and self.col == self.pattern.steps + 1
        t = self.project.audio_track(self.spec, "the grid")
        if not t.get("instrument"):
            raise GoutError(f"track {t['n']} {t['name']} is a recording, not an instrument: nothing to show in a grid")
        self.t = t
        self.spec = str(t["n"])
        self.item = instrument(t["instrument"]["kind"])
        if self.item is None:
            raise GoutError(f"track {t['n']} {t['name']} is played by {t['instrument']['kind']}, which is not installed")
        self.settings = t["instrument"]["settings"]
        self.pattern = Pattern.from_json(t["instrument"]["pattern"], self.item.features)
        self.ctx = context(self.project, self.item, self.settings)
        try:
            self.reads = self.item.reads(self.ctx, self.pattern, self.settings)
        except Exception as exc:  # a row nobody types must not take the grid down
            self.reads = []
            self.message = f"{self.item.name}: {type(exc).__name__}: {exc}"
        own = [f for f in self.item.settings
               if f.name in self.settings or f.name in self.extra or self.item.shown is None or f.name in self.item.shown]
        self.cells: list[Feature] = [*BASE_SETTINGS, *own]
        self.rows: list[Feature] = list(self.item.features)
        self.row = max(0, min(self.row, len(self.rows)))
        self.col = self.pattern.steps + 1 if on_more else max(0, min(self.col, self.last_col()))   # the + stays under the cursor

    def last_col(self) -> int:
        return len(self.cells) if self.row == 0 else self.pattern.steps + 1

    def addable(self) -> list[Feature]:
        return [f for f in self.item.settings if f not in self.cells]

    def here(self) -> tuple[str, Feature | None, int | None]:
        """What the cursor is on: (cell, feature, None), (all, feature, None), (step, feature, n), (more, ...)."""
        if self.row == 0:
            return ("cell", self.cells[self.col], None) if self.col < len(self.cells) else ("more-cells", None, None)
        feature = self.rows[self.row - 1]
        if self.col == 0:
            return "all", feature, None
        return ("step", feature, self.col) if self.col <= self.pattern.steps else ("more-steps", None, None)

    def cell_value(self, feature: Feature):
        if feature.name == "steps":
            return self.pattern.steps
        if feature.name == "title":
            return self.settings.get("title") or self.t["name"]
        return self.item.value(self.settings, feature.name)

    def text_here(self) -> str:
        what, feature, step = self.here()
        if what == "cell":
            return feature.format(self.cell_value(feature))
        if what == "all":
            return self.all_text(feature)
        if what == "step":
            own = self.pattern.cell(feature, step)
            return "" if own is None else feature.format(own)
        return ""

    def all_text(self, feature: Feature) -> str:
        given = self.pattern.rows.get(feature.name, {}).get("all")
        return "" if given is None and feature.default is None else feature.format(self.pattern.every(feature))

    def wants_space(self) -> bool:
        """Whether a space is a character here (a title being typed) rather than play and stop."""
        what, feature, _ = self.here()
        return self.edit is not None and what == "cell" and feature.kind == "text"

    # ---- keys

    def key(self, name: str) -> tuple | None:
        """A key: left right up down home end tab btab enter backspace delete pgup pgdn esc, or a
        character. Returns None, ("run", argv) for the ui to run, or ("close",)."""
        self.message = ""
        if self.choosing is not None:
            return self.key_choosing(name)
        typing = self.edit is not None
        what, feature, _ = self.here()
        if name == "esc":
            if typing:
                self.edit = None
                return None
            return ("close",)
        if name in MOVES:
            run = self.commit() if typing else None
            self.move(name)
            return run
        if name == "enter":
            if typing:
                return self.commit()
            if what == "more-steps":
                return ("run", ["instrument", self.spec, "steps", "+1"])
            if what == "more-cells":
                self.choosing, self.choice = self.addable(), 0
                if not self.choosing:
                    self.choosing, self.message = None, f"the {self.item.name} has no more cells to add"
                return None
            self.edit = self.text_here()   # the value as it stands, to change
            return None
        if name == "backspace" and typing:
            self.edit = self.edit[:-1]
            return None
        if name in ("backspace", "delete"):
            self.edit = None
            return self.write("-") if what in ("cell", "all", "step") else None
        if name in ("pgup", "pgdn"):
            return None if typing else self.nudge(12 if name == "pgup" else -12)
        if len(name) == 1 and name.isprintable():
            if not typing and name in "+-" and what in ("cell", "all", "step") and feature.kind != "text":
                return self.nudge(1 if name == "+" else -1)
            if (not typing and what in ("all", "step") and feature.kind == "choice" and name in feature.choices
                    and all(len(c) == 1 for c in feature.choices)):
                run = self.write(name)          # a row of single letters (x, o): the letter is the whole edit
                if what == "step":
                    self.move("right")
                return run
            self.edit = (self.edit or "") + name
        return None

    def key_choosing(self, name: str) -> tuple | None:
        if name == "esc":
            self.choosing = None
        elif name in ("up", "down"):
            self.choice = (self.choice + (1 if name == "down" else -1)) % len(self.choosing)
        elif name == "enter":
            added = self.choosing[self.choice]
            self.extra.append(added.name)
            self.choosing = None
            self.load()
            self.row, self.col = 0, self.cells.index(added)
            self.edit = added.format(self.cell_value(added))   # ready to be given a value
        return None

    def move(self, name: str) -> None:
        if self.row == 0:
            self.col_first = self.col
        else:
            self.col_steps = self.col
        last = self.last_col()
        if name == "left":
            self.col = max(0, self.col - 1)
        elif name == "right":
            self.col = min(last, self.col + 1)
        elif name == "home":
            self.col = 0
        elif name == "end":
            self.col = last - 1          # the last cell with something in it; the + is one further
        elif name == "tab":              # the next cell, row after row, and round to the top
            if self.col < last:
                self.col += 1
            else:
                self.row, self.col = (self.row + 1) % (len(self.rows) + 1), 0
        elif name == "btab":
            if self.col > 0:
                self.col -= 1
            else:
                self.row = (self.row - 1) % (len(self.rows) + 1)
                self.col = self.last_col()
        elif name in ("up", "down"):
            new = max(0, min(len(self.rows), self.row + (1 if name == "down" else -1)))
            if new != self.row:
                if new == 0:
                    self.col = min(self.col_first, len(self.cells))
                elif self.row == 0:
                    self.col = min(self.col_steps, self.pattern.steps + 1)
                self.row = new

    def commit(self) -> tuple | None:
        text, self.edit = (self.edit or "").strip(), None
        what, feature, _ = self.here()
        if not text:
            return None
        if what == "more-steps":
            if not text.lstrip("+").isdigit():
                self.message = f"how many steps to add: a number, not {text!r}"
                return None
            return ("run", ["instrument", self.spec, "steps", "+" + text.lstrip("+")])
        if what == "more-cells":
            return None
        if feature.kind == "choice" and text.lower() not in feature.choices:   # s for soft, h for hard
            starts = [c for c in feature.choices if c.startswith(text.lower())]
            if len(starts) == 1:
                text = starts[0]
        return self.write(text)

    def write(self, text: str) -> tuple:
        what, feature, step = self.here()
        if what == "cell":
            return ("run", ["instrument", self.spec, feature.name, text])
        return ("run", ["instrument", self.spec, feature.name, "all" if what == "all" else str(step), text])

    def nudge(self, by: int) -> tuple | None:
        """The cell a little up or down (+ and -), or a lot (page up and down)."""
        what, feature, step = self.here()
        if what not in ("cell", "all", "step"):
            return None
        value = self.cell_value(feature) if what == "cell" else (
            self.pattern.every(feature) if what == "all" else self.pattern.value(feature, step))
        big = abs(by) > 1
        up = 1 if by > 0 else -1
        if feature.kind == "note":
            if not isinstance(value, int):
                self.message = "no note here to move: type one, like C2"
                return None
            new = note_name(max(0, min(127, value + (12 if big else 1) * up)))
        elif feature.kind == "count":
            new = str(max(1, value + (4 if big else 1) * up))
        elif feature.kind == "number":
            lo, hi = feature.lo, feature.hi
            unit = 1.0
            if lo is not None and hi is not None:   # a hundredth of the range, as a round number
                unit = 10 ** round(_log10((hi - lo) / 100))
            new = (value or 0.0) + unit * (10 if big else 1) * up
            if lo is not None:
                new = max(lo, new)
            if hi is not None:
                new = min(hi, new)
            new = f"{round(new, 6):g}"
        elif feature.kind == "choice":
            options = [c for c in feature.choices if c not in (HOLD, REST)] or list(feature.choices)
            new = options[(options.index(value) + up) % len(options)] if value in options else options[0]
        elif feature.kind == "toggle":
            new = "off" if value else "on"
        else:
            return None
        return self.write(new)

    # ---- the screen

    def headline(self) -> str:
        loop = self.item.value(self.settings, "loop")
        return (f" {self.t['name']}  {self.item.name}  {self.pattern.steps} steps of {self.item.value(self.settings, 'step')}"
                f" at {self.ctx.bpm:g} bpm = {self.pattern.steps * self.ctx.step_s:.3f} s" + (f", {loop} times" if loop > 1 else ""))

    def lines(self, width: int, height: int, playing: int | None = None) -> tuple[list[list[tuple[int, str, str]]], tuple[int, int]]:
        """The screen: rows of (column, text, role), and where the cursor goes (row, column).
        Roles: head, hint, label, cell, empty, read, cursor, edit, play, beat, note, error, list, picked."""
        out: list[list[tuple[int, str, str]]] = []
        cursor = (0, 0)
        hint = "ctrl-y next instrument  esc back " if len(self.headline()) + 35 <= width else "esc back "
        out.append([(0, self.headline()[:max(0, width - len(hint))].ljust(max(0, width - len(hint))) + hint, "head")])
        # the first row, wrapped
        row, x = [], 1
        for k, feature in enumerate([*self.cells, None]):
            on = self.row == 0 and self.col == k
            if feature is None:
                text = "+"
            elif on and self.edit is not None:
                text = f"{feature.name} {self.edit}"
            else:
                text = f"{feature.name} {feature.format(self.cell_value(feature))}".rstrip()
            if x + len(text) > width - 1 and row:
                out.append(row)
                row, x = [], 1
            role = ("edit" if self.edit is not None else "cursor") if on else ("label" if feature is None else "cell")
            row.append((x, text, role))
            if on:
                cursor = (len(out), min(width - 1, x + len(text)))
            x += len(text) + 3
        out.append(row)
        if self.choosing is not None:
            for k, feature in enumerate(self.choosing[:max(1, height - len(out) - 3)]):
                out.append([(3, f"{feature.name:<14} {feature.summary}"[:width - 4], "picked" if k == self.choice else "list")])
            out.append([(1, "up and down choose, enter adds the cell, esc leaves it", "hint")])
            return out, cursor
        out.append([])
        # the rows: names, the value for every step, then as many beats as fit
        names = [f.name for f in self.rows] + [name for name, _ in self.reads]
        texts: dict[str, list[str]] = {}
        for feature in self.rows:
            texts[feature.name] = [EMPTY if self.pattern.cell(feature, s) is None else feature.format(self.pattern.cell(feature, s))
                                   for s in range(1, self.pattern.steps + 1)]
        for name, cells in self.reads:
            texts[name] = (list(cells) + [""] * self.pattern.steps)[:self.pattern.steps]
        every = {f.name: self.all_text(f) for f in self.rows}
        name_w = max(len(n) for n in names)
        typed = len(self.edit) if self.edit is not None and self.row else 0   # room for what is being typed
        all_w = max([3, typed if self.col == 0 else 0] + [len(v) for v in every.values()])
        cell_w = max([3, typed if self.col else 0] + [len(c) for cells in texts.values() for c in cells]) + 1
        left = 1 + name_w + 1 + all_w + 3
        beats = max(1, (width - left - 3) // (4 * cell_w + 3))
        page = beats * 4
        first = ((max(1, min(self.col, self.pattern.steps)) - 1) // page) * page + 1 if self.row else 1
        last = min(self.pattern.steps, first + page - 1)

        def column(step: int) -> int:
            return left + (step - first) * cell_w + ((step - first) // 4) * 3

        header = [(1 + name_w + 1, "all".rjust(all_w), "label")]
        for step in range(first, last + 1):
            header.append((column(step), str(step).rjust(cell_w), "play" if step == playing else "label"))
        more_x = column(last) + cell_w + 1 if last == self.pattern.steps else None
        if more_x is not None:
            on = self.row and self.col == self.pattern.steps + 1
            header.append((more_x, f"+{self.edit}" if on and self.edit else "+", ("edit" if self.edit is not None else "cursor") if on else "label"))
            if on:
                cursor = (len(out), min(width - 1, more_x + 1 + len(self.edit or "")))
        elif last < self.pattern.steps:
            header.append((column(last) + cell_w + 1, f"… {self.pattern.steps}", "hint"))
        out.append(header)
        for r, name in enumerate(names, 1):
            feature = self.rows[r - 1] if r <= len(self.rows) else None
            line = [(1, name.ljust(name_w), "label")]
            if feature is not None:
                on = self.row == r and self.col == 0
                shown = self.edit if on and self.edit is not None else every[name]
                line.append((1 + name_w + 1, shown.rjust(all_w), ("edit" if self.edit is not None else "cursor") if on else "cell"))
                if on:
                    cursor = (len(out), min(width - 1, 1 + name_w + 1 + max(all_w, len(shown))))
            for step in range(first, last + 1):
                if (step - first) % 4 == 0:
                    line.append((column(step) - 2 if step > first else left - 2, "│", "beat"))
                text = texts[name][step - 1]
                on = feature is not None and self.row == r and self.col == step
                if on and self.edit is not None:
                    text = self.edit
                role = (("edit" if self.edit is not None else "cursor") if on else "read" if feature is None
                        else "play" if step == playing else "empty" if text == EMPTY else "note")
                line.append((column(step), text.rjust(cell_w), role))
                if on:
                    cursor = (len(out), min(width - 1, column(step) + cell_w))
            out.append(line)
        while len(out) < height - 2:
            out.append([])
        out = out[:max(1, height - 2)]
        out.append([(0, HINT[:width - 1], "hint")])
        out.append([(0, self.status()[:width - 1], "error" if self.message else "label")])
        return out, cursor

    def status(self) -> str:
        if self.message:
            return " " + self.message
        what, feature, step = self.here()
        if what == "more-steps":
            return " enter adds a step; a number and enter adds that many"
        if what == "more-cells":
            return f" enter adds one of the {self.item.name}'s other cells"
        about = feature.summary
        if feature.kind == "choice":
            about += f"  ({' '.join(feature.choices)})"
        elif feature.kind in ("number", "count") and (feature.lo is not None or feature.hi is not None):
            about += f"  ({feature.span()})"
        where = "the first row" if what == "cell" else "every step" if what == "all" else f"step {step}"
        return f" {feature.name}, {where}: {about}"


def _log10(value: float) -> float:
    import math
    return math.log10(value) if value > 0 else 0.0
