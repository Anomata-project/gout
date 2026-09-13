"""Delay: settings line with tempo-relative times, ffmpeg filter, picture of the repeats."""
from __future__ import annotations

import math
import re

from ..core import die, GoutError
from ..fx import Effect, FxContext, GUTTER as EQ_GUTTER


#
# A delay line:  375ms w30 f40 n4   or with a tempo set:  1/8 w30 f40 n4   (time, wet %, feedback %,
# repeats). Note values: 1/4 1/8 1/16 3/16, a trailing d for dotted, t for triplet.
DELAY_DEFAULTS = {"wet": 30.0, "feedback": 40.0, "repeats": 4}


DELAY_PRESETS = {
    "slap":    ("80ms w25 f0 n1", "one quick repeat"),
    "eighth":  ("1/8 w30 f35 n4", "eighth notes, needs a bpm"),
    "quarter": ("1/4 w30 f40 n4", "quarter notes, needs a bpm"),
    "dotted":  ("3/16 w30 f40 n4", "dotted eighths, the classic"),
    "long":    ("500ms w25 f50 n6", "half a second, six repeats"),
}


DELAY_SYNTAX = "375ms w30 f40 n4   (time or a note value like 1/8 with a bpm set; wet %, feedback %, repeats)"


def parse_delay(text: str) -> dict:
    tokens: list[str] = []
    for tok in text.split():
        if tok.lower() in DELAY_PRESETS:
            tokens += DELAY_PRESETS[tok.lower()][0].split()
        else:
            tokens.append(tok)
    d: dict = {"time": None, **DELAY_DEFAULTS}
    for tok in tokens:
        t = tok.lower()
        if re.fullmatch(r"\d+/\d+[dt]?", t):
            d["time"] = t  # a note value, resolved against the bpm when the filter is built
        elif re.fullmatch(r"\d+(?:\.\d+)?(ms|s)", t):
            ms = float(t[:-2]) if t.endswith("ms") else float(t[:-1]) * 1000
            if not 1 <= ms <= 5000:
                raise ValueError(f"delay time {tok} is outside 1 ms .. 5 s")
            d["time"] = f"{ms:g}ms"
        elif re.fullmatch(r"w\d+(?:\.\d+)?", t):
            d["wet"] = float(t[1:])
        elif re.fullmatch(r"f\d+(?:\.\d+)?", t):
            d["feedback"] = float(t[1:])
        elif re.fullmatch(r"n\d+", t):
            d["repeats"] = int(t[1:])
        else:
            raise ValueError(f"bad delay setting {tok!r}; a line looks like  {DELAY_SYNTAX}  (or a preset: delay presets)")
    if d["time"] is None:
        raise ValueError(f"a delay needs a time: {DELAY_SYNTAX}")
    if not 0 <= d["wet"] <= 100:
        raise ValueError("wet is a percentage, 0 .. 100")
    if not 0 <= d["feedback"] <= 95:
        raise ValueError("feedback is a percentage, 0 .. 95")
    if not 1 <= d["repeats"] <= 8:
        raise ValueError("repeats: 1 .. 8")
    return d


def fmt_delay(d: dict) -> str:
    return f"{d['time']} w{d['wet']:g} f{d['feedback']:g} n{d['repeats']}"


def delay_ms(d: dict, bpm: float | None) -> float:
    """The delay time in ms; note values need the project's bpm."""
    t = d["time"]
    if t.endswith("ms"):
        return float(t[:-2])
    m = re.fullmatch(r"(\d+)/(\d+)([dt]?)", t)
    if bpm is None:
        die(f"the delay time {t} is a note value: set bpm 120 first, or give the time in ms")
    ms = int(m.group(1)) / int(m.group(2)) * 4 * 60000 / bpm
    return ms * {"d": 1.5, "t": 2 / 3, "": 1}[m.group(3)]


def delay_taps(d: dict, bpm: float | None) -> list[tuple[float, float]]:
    """(time ms, level 0..1) of each repeat."""
    ms = delay_ms(d, bpm)
    wet, fb = d["wet"] / 100, d["feedback"] / 100
    taps = []
    for k in range(1, d["repeats"] + 1):
        level = wet * (fb ** (k - 1))
        if level < 0.001 or k * ms > 90000:
            break
        taps.append((k * ms, min(1.0, level)))
    return taps


def delay_filter(d: dict, bpm: float | None) -> str | None:
    taps = delay_taps(d, bpm)
    if not taps:
        return None
    return ("aecho=1:1:" + "|".join(f"{ms:g}" for ms, _ in taps)
            + ":" + "|".join(f"{lvl:.4f}" for _, lvl in taps))


def render_delay(d: dict | None, bpm: float | None, width: int = 60, height: int = 6) -> list[tuple[str, str, str]]:
    """The dry hit and its repeats over time, bar height by level in dB (0 .. -48)."""
    gw = max(12, width - EQ_GUTTER)
    try:
        taps = delay_taps(d, bpm) if d else []
    except GoutError:
        taps = []
    total = (taps[-1][0] * 1.15) if taps else 1000.0
    cells = [[" "] * gw for _ in range(height)]
    classes = [[" "] * gw for _ in range(height)]
    sub = 2 * height

    def bar(col: int, level: float, cls: str) -> None:
        db = 20 * math.log10(max(level, 1e-4))
        top_sub = round(min(1.0, max(0.0, -db / 48)) * (sub - 1))
        for row in range(height):
            if 2 * row + 1 >= top_sub:
                cells[row][col] = "█" if 2 * row >= top_sub else "▄"
                classes[row][col] = cls

    bar(0, 1.0, "z")
    for ms, level in taps:
        bar(max(1, min(gw - 1, round(ms / total * (gw - 1)))), level, "a")
    head = f"delay {fmt_delay(d) if d else 'none'}"
    if d and taps:
        head += f"  = {delay_ms(d, bpm):.0f} ms" + (f" at {bpm:g} bpm" if bpm and not d["time"].endswith("ms") else "")
    rows = [(head, "", "head")]
    for row in range(height):
        label = "0" if row == 0 else ("-48" if row == height - 1 else ("-24" if row == height // 2 else ""))
        rows.append((f"{label:>{EQ_GUTTER - 1}} " + "".join(cells[row]), " " * EQ_GUTTER + "".join(classes[row]), "graph"))
    axis = [" "] * gw
    for ms in ([0] + [tap[0] for tap in taps])[:8]:
        text = f"{ms:.0f}"
        col = max(0, min(gw - len(text), round(ms / total * (gw - 1)) - (0 if ms == 0 else len(text) // 2)))
        if all(ch == " " for ch in axis[max(0, col - 1):col + len(text) + 1]):
            axis[col:col + len(text)] = list(text)
    rows.append((" " * EQ_GUTTER + "".join(axis) + "  ms", "", "axis"))
    return rows


def needs_bpm(d: dict) -> bool:
    return not d["time"].endswith("ms")


class DelayEffect(Effect):
    name = "delay"
    aliases = ("dl", "echo")
    summary = "delay: time (or a note value with a bpm), wet %, feedback %, repeats"
    syntax = "375ms w30 f40 n4"
    hint = "375ms w30 f40 n4 | 1/8 | slap"
    presets = DELAY_PRESETS
    order = 30
    picture_width = (40, 80)
    picture_height = 6
    cheat = (
        ("", "", "1/8 1/8d 1/8t", "note values, plain, dotted, triplet; needs set bpm 120"),
    )
    help = (f"settings: {DELAY_SYNTAX}",
            "with  set bpm 120  the time can be a note value: 1/4 1/8 1/16 3/16, 1/8d dotted, 1/8t triplet")

    def parse(self, text: str) -> dict:
        return parse_delay(text)

    def format(self, params: dict) -> str:
        return fmt_delay(params)

    def check(self, ctx: FxContext, params: dict) -> None:
        if needs_bpm(params) and ctx.bpm is None:
            die(f"the delay time {params['time']} is a note value: set bpm 120 first, or give the time in ms")

    def filters(self, ctx: FxContext, params: dict) -> list[str]:
        echo = delay_filter(params, ctx.bpm)
        return [echo] if echo else []

    def tail_ms(self, ctx: FxContext, params: dict) -> int:
        if needs_bpm(params) and ctx.bpm is None:
            return 0
        taps = delay_taps(params, ctx.bpm)
        return math.ceil(taps[-1][0]) if taps else 0

    def picture(self, ctx: FxContext, params: dict | None, width: int, height: int):
        return render_delay(params, ctx.bpm, width, height)
