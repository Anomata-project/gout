"""A pattern: steps across, a row per feature, as an instrument track stores it.

Pure data and text: no project, no audio. A row holds one value for every step ("all") and the
steps that differ from it; a note row with nothing in a step is a rest there.

    C2 d#3 Bb1      notes, by name and octave (A4 is 440 Hz)
    =               hold: the note before goes on through this step
    rest            silence here, whatever the row says for every step
    - or .          nothing of its own: the step takes the row's value
"""
from __future__ import annotations

import math
import re

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_RE = re.compile(r"([a-g])([#b]?)(-?\d)")
SEMITONE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
HOLD, REST = "=", "rest"
EMPTY_WORDS = ("-", ".", "", "clear", "none")
MAX_STEPS = 256
DIVISIONS = {"1/4": 1.0, "1/8": 2.0, "1/8t": 3.0, "1/16": 4.0, "1/16t": 6.0, "1/32": 8.0}  # steps to a beat


def parse_note(text: str) -> int:
    """A note name as a MIDI number: C4 is 60, A4 is 69."""
    m = NOTE_RE.fullmatch(text.strip().lower())
    if not m:
        raise ValueError(f"{text!r} is not a note: a letter, # or b when wanted, and an octave, like C2 or Bb3")
    midi = 12 * (int(m.group(3)) + 1) + SEMITONE[m.group(1)] + {"#": 1, "b": -1, "": 0}[m.group(2)]
    if not 0 <= midi <= 127:
        raise ValueError(f"{text!r} is out of range: C-1 to G9")
    return midi


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def note_hz(midi: float, a4: float = 440.0) -> float:
    return a4 * 2 ** ((midi - 69) / 12)


def hz_text(hz: float) -> str:
    """A frequency as the nearest note and how far off it is, in cents: A4+12."""
    semis = 69 + 12 * math.log2(hz / 440.0)
    nearest = round(semis)
    off = round((semis - nearest) * 100)
    return note_name(nearest) + (f"{off:+d}" if off else "")


class Feature:
    """One row of the grid, or one cell of its first row: what it may hold and how that reads.

    kind: note (a pitch, a hold or a rest), number (lo .. hi, written with its unit or without),
    count (a whole number), toggle (on or off), choice (one of choices) or text.
    """

    def __init__(self, name: str, kind: str = "number", default=None, lo: float | None = None, hi: float | None = None,
                 unit: str = "", choices: tuple[str, ...] = (), summary: str = ""):
        self.name, self.kind, self.default = name, kind, default
        self.lo, self.hi, self.unit, self.choices, self.summary = lo, hi, unit, tuple(choices), summary

    def parse(self, text: str):
        """The value a cell's text stands for. ValueError says what it should have been."""
        word = text.strip()
        if self.kind == "text":
            return word
        low = word.lower()
        if self.kind == "note":
            return low if low in (HOLD, REST) else parse_note(word)
        if self.kind == "toggle":
            if low in ("on", "x", "1", "yes"):
                return True
            if low in ("off", "0", "no"):
                return False
            raise ValueError(f"{self.name} is on or off, not {text!r}")
        if self.kind == "choice":
            if low not in self.choices:
                raise ValueError(f"{self.name} is one of {', '.join(self.choices)}, not {text!r}")
            return low
        if self.kind == "count":
            if not low.isdigit() or (self.lo is not None and int(low) < self.lo) or (self.hi is not None and int(low) > self.hi):
                raise ValueError(f"{self.name} is a whole number {self.span()}, not {text!r}")
            return int(low)
        number = low.removesuffix(self.unit.lower()) if self.unit else low
        scale = 1.0
        if number.endswith("k") and number[:-1].replace(".", "", 1).isdigit():  # 2k, 1.5k
            number, scale = number[:-1], 1000.0
        try:
            value = float(number) * scale
        except ValueError:
            raise ValueError(f"{self.name} is a number{' in ' + self.unit if self.unit else ''}, not {text!r}") from None
        if not math.isfinite(value) or (self.lo is not None and value < self.lo) or (self.hi is not None and value > self.hi):
            raise ValueError(f"{self.name} goes {self.span()}, not {text!r}")
        return value

    def span(self) -> str:
        """The range of a number, in words: from 0 to 20000 ms."""
        unit = f" {self.unit}" if self.unit else ""
        if self.lo is not None and self.hi is not None:
            return f"from {self.lo:g} to {self.hi:g}{unit}"
        if self.lo is not None:
            return f"from {self.lo:g}{unit} up"
        return f"up to {self.hi:g}{unit}" if self.hi is not None else "anywhere"

    def format(self, value) -> str:
        """The text of a value, as parse reads it back."""
        if value is None:
            return ""
        if self.kind == "note":
            return value if isinstance(value, str) else note_name(value)
        if self.kind == "toggle":
            return "on" if value else "off"
        if self.kind == "number":
            return f"{value:g}"
        return str(value)

    def check(self, value):
        """A stored value (from gout.json) as this feature holds it, or ValueError."""
        if value is None:
            return None
        if self.kind == "note" and isinstance(value, int) and not isinstance(value, bool):
            if not 0 <= value <= 127:
                raise ValueError(f"{self.name}: note {value} is out of range")
            return value
        if self.kind == "toggle" and isinstance(value, bool):
            return value
        if self.kind in ("number", "count") and isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.parse(f"{value:g}")
        if isinstance(value, str):
            return self.parse(value)
        raise ValueError(f"{self.name}: cannot hold {value!r}")


NOTE = Feature("note", "note", summary="the pitch: C2, d#3, Bb1; = holds the note before, rest is silence")
DECAY = Feature("decay", "number", 400.0, 0, 20000, "ms", summary="how long the note takes to die away; 0 keeps it at full level")
RELEASE = Feature("release", "number", 50.0, 0, 20000, "ms", summary="how long it rings on after its step ends")


def parse_steps(text: str, steps: int) -> list[int] | None:
    """Which steps a word names: 3, 5-8, 1,5,9 or all (None: the row's value for every step)."""
    word = text.strip().lower()
    if word == "all":
        return None
    out: list[int] = []
    for piece in word.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", piece)
        if not m:
            raise ValueError(f"{text!r} is not a step: 3, 5-8, 1,5,9 or all")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= first <= last <= steps:
            raise ValueError(f"step {piece}: the pattern has steps 1 to {steps}")
        out += range(first, last + 1)
    return sorted(set(out))


class Pattern:
    """The steps and rows of one instrument track."""

    def __init__(self, steps: int = 16, rows: dict | None = None):
        self.steps = steps
        self.rows: dict[str, dict] = rows or {}   # feature name -> {"all": value or None, "cells": {step: value}}

    @classmethod
    def from_json(cls, data: dict | None, features: tuple[Feature, ...] | None = None) -> "Pattern":
        """A stored pattern. With features, every value is checked and rows nobody knows are dropped;
        ValueError when it is not a pattern at all."""
        data = data or {}
        steps = data.get("steps", 16)
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_STEPS:
            raise ValueError(f"steps {steps!r}: a whole number from 1 to {MAX_STEPS}")
        known = {f.name: f for f in features} if features is not None else None
        rows: dict[str, dict] = {}
        for name, row in (data.get("rows") or {}).items():
            if not isinstance(row, dict) or (known is not None and name not in known):
                continue
            feature = known[name] if known is not None else None
            every = row.get("all")
            cells = {}
            for key, value in (row.get("cells") or {}).items():
                if not str(key).isdigit() or not 1 <= int(key) <= steps or value is None:
                    continue
                cells[int(key)] = feature.check(value) if feature else value
            rows[name] = {"all": feature.check(every) if feature else every, "cells": cells}
        return cls(steps, rows)

    def to_json(self) -> dict:
        rows = {}
        for name, row in sorted(self.rows.items()):
            if row["all"] is None and not row["cells"]:
                continue
            rows[name] = {"all": row["all"], "cells": {str(k): v for k, v in sorted(row["cells"].items())}}
        return {"steps": self.steps, "rows": rows}

    def row(self, name: str) -> dict:
        return self.rows.setdefault(name, {"all": None, "cells": {}})

    def cell(self, feature: Feature, step: int):
        """What a step holds of its own in a row, or None."""
        return self.rows.get(feature.name, {"cells": {}})["cells"].get(step)

    def every(self, feature: Feature):
        """The row's value for every step: the one given, else the feature's own."""
        given = self.rows.get(feature.name, {}).get("all")
        return feature.default if given is None else given

    def value(self, feature: Feature, step: int):
        """What sounds at a step: its own cell, else the row's value."""
        own = self.cell(feature, step)
        return self.every(feature) if own is None else own

    def put(self, feature: Feature, steps: list[int] | None, value) -> None:
        """Set cells (or the row's value, steps None); value None empties them."""
        row = self.row(feature.name)
        if steps is None:
            row["all"] = value
            return
        for step in steps:
            if value is None:
                row["cells"].pop(step, None)
            else:
                row["cells"][step] = value

    def resize(self, steps: int) -> int:
        """Change the length; cells past the new end go. Returns how many were dropped."""
        dropped = 0
        for row in self.rows.values():
            for step in [s for s in row["cells"] if s > steps]:
                del row["cells"][step]
                dropped += 1
        self.steps = steps
        return dropped

    def notes(self, feature: Feature = NOTE) -> list[tuple[int, int, int]]:
        """(step, MIDI note, steps it lasts) for every note that starts: a hold lengthens the note
        before it, a rest or an empty step ends it."""
        out: list[list[int]] = []
        sounding = False
        for step in range(1, self.steps + 1):
            value = self.value(feature, step)
            if value == HOLD:
                if sounding:
                    out[-1][2] += 1
                continue
            sounding = isinstance(value, int)
            if sounding:
                out.append([step, value, 1])
        return [tuple(item) for item in out]


def step_seconds(bpm: float, division: str = "1/16") -> float:
    return 60.0 / bpm / DIVISIONS[division]


def step_edges(steps: int, step_s: float, rate: int) -> list[int]:
    """The sample each step starts at, and where the last one ends: rounded from the whole
    distance each time, so the steps never drift."""
    return [round(i * step_s * rate) for i in range(steps + 1)]
