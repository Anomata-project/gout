"""Compressor: settings line, ffmpeg filter, transfer curve and its picture."""
from __future__ import annotations

import math
import re

from ..fx import Effect, FxContext, GUTTER as EQ_GUTTER


#
# A track's compressor is one line:  -18 4:1 a10 r120 k6 m3   (threshold dB, ratio, attack ms,
# release ms, knee dB, makeup dB). Presets stand for such lines.
COMP_DEFAULTS = {"threshold": -18.0, "ratio": 2.0, "attack": 20.0, "release": 200.0, "knee": 6.0, "makeup": 0.0}


COMP_PRESETS = {
    "gentle": ("-18 2:1 a20 r200 k6", "barely there: evens things out"),
    "vocal":  ("-20 3:1 a5 r120 k4 m4", "keeps a voice in front"),
    "drums":  ("-14 4:1 a10 r80 k2 m3", "punch: lets the transient through, grabs the rest"),
    "bass":   ("-16 3:1 a15 r150 k4 m3", "steady low end"),
    "glue":   ("-16 2:1 a30 r300 k8 m2", "slow and soft, for a whole part"),
    "squash": ("-24 8:1 a2 r60 k1 m8", "flat and loud"),
    "limit":  ("-6 20:1 a0.5 r50 k1", "catches peaks only"),
}


COMP_SYNTAX = "-18 4:1 a10 r120 k6 m3   (threshold dB, ratio, attack ms, release ms, knee dB, makeup dB or mauto)"


def parse_comp(text: str) -> dict:
    """Parameters from a line of tokens; unspecified ones take the defaults."""
    tokens: list[str] = []
    for tok in text.split():
        if tok.lower() in COMP_PRESETS:
            tokens += COMP_PRESETS[tok.lower()][0].split()
        else:
            tokens.append(tok)
    c = dict(COMP_DEFAULTS)
    auto = False
    for tok in tokens:
        t = tok.lower()
        if re.fullmatch(r"-\d+(?:\.\d+)?(?:db)?", t) or t in ("0", "0db"):
            c["threshold"] = float(t.removesuffix("db"))
        elif re.fullmatch(r"\d+(?:\.\d+)?(?::1|x)?", t):
            c["ratio"] = float(t.removesuffix(":1").removesuffix("x"))
        elif re.fullmatch(r"a\d+(?:\.\d+)?", t):
            c["attack"] = float(t[1:])
        elif re.fullmatch(r"r\d+(?:\.\d+)?", t):
            c["release"] = float(t[1:])
        elif re.fullmatch(r"k\d+(?:\.\d+)?", t):
            c["knee"] = float(t[1:])
        elif t == "mauto":
            auto = True
        elif re.fullmatch(r"m\+?\d+(?:\.\d+)?", t):
            c["makeup"] = float(t[1:])
        else:
            raise ValueError(f"bad compressor setting {tok!r}; a line looks like  {COMP_SYNTAX}"
                             "  (or a preset: comp presets)")
    if not -60 <= c["threshold"] <= 0:
        raise ValueError("threshold must be between -60 and 0 dB")
    if not 1 <= c["ratio"] <= 20:
        raise ValueError("ratio must be between 1 and 20")
    if not 0.01 <= c["attack"] <= 2000:
        raise ValueError("attack must be between 0.01 and 2000 ms")
    if not 0.01 <= c["release"] <= 9000:
        raise ValueError("release must be between 0.01 and 9000 ms")
    if not 1 <= c["knee"] <= 8:
        raise ValueError("knee must be between 1 and 8 dB")
    if auto:  # half of what a full-scale peak would lose
        c["makeup"] = round(-c["threshold"] * (1 - 1 / c["ratio"]) / 2, 1)
    if not 0 <= c["makeup"] <= 36:
        raise ValueError("makeup must be between 0 and 36 dB (ffmpeg only adds gain here; use gain to cut)")
    return c


def fmt_comp(c: dict) -> str:
    out = f"{c['threshold']:g} {c['ratio']:g}:1 a{c['attack']:g} r{c['release']:g} k{c['knee']:g}"
    return out + (f" m{c['makeup']:g}" if c["makeup"] else "")


def comp_filter(c: dict) -> str:
    return (f"acompressor=threshold={10 ** (c['threshold'] / 20):.6f}:ratio={c['ratio']:g}"
            f":attack={c['attack']:g}:release={c['release']:g}:knee={c['knee']:g}"
            f":makeup={10 ** (c['makeup'] / 20):.4f}")


def comp_out(c: dict, x: float, makeup: bool = True) -> float:
    """Static transfer curve: output level for an input level x, both in dB, soft knee."""
    thr, ratio, w = c["threshold"], c["ratio"], c["knee"]
    if x < thr - w / 2:
        y = x
    elif x > thr + w / 2:
        y = thr + (x - thr) / ratio
    else:
        y = x + (1 / ratio - 1) * (x - thr + w / 2) ** 2 / (2 * w)
    return y + (c["makeup"] if makeup else 0)


def comp_estimate(c: dict, peaks: bytes) -> tuple[float, float, float] | None:
    """(share of the time it works, average and deepest reduction in dB) on a track's peaks."""
    levels = [20 * math.log10(v / 128) for v in peaks if v > 0]
    if not levels:
        return None
    reductions = [comp_out(c, x, makeup=False) - x for x in levels]
    working = [g for g in reductions if g < -0.1]
    if not working:
        return (0.0, 0.0, 0.0)
    return (len(working) / len(levels), sum(working) / len(working), min(working))


COMP_W = 30  # columns of the transfer plot including the gutter


def render_comp(c: dict | None, width: int = COMP_W, height: int = 8,
                peaks: bytes | None = None) -> list[tuple[str, str, str]]:
    """Rows of (text, classes, kind): input dB left to right, output dB bottom to top,
    both -60 .. 0. Classes: a the curve, z the unity line, x the track's level histogram."""
    gw = max(10, width - EQ_GUTTER)
    sub = 2 * height
    xs = [-60 + 60 * col / (gw - 1) for col in range(gw)]

    def ysub(db: float) -> int:
        db = max(-60.0, min(0.0, db))
        return max(0, min(sub - 1, round(-db / 60 * (sub - 1))))

    cells = [[" "] * gw for _ in range(height)]
    classes = [[" "] * gw for _ in range(height)]
    if peaks:
        counts = [0] * gw
        for v in peaks:
            if v > 0:
                db = 20 * math.log10(v / 128)
                counts[max(0, min(gw - 1, round((db + 60) / 60 * (gw - 1))))] += 1
        top_count = max(counts) or 1
        for col in range(gw):
            level = counts[col] / top_count
            if level > 0:
                top_sub = round((1 - level) * (sub - 1))
                for row in range(height):
                    if 2 * row >= top_sub:
                        cells[row][col], classes[row][col] = "░", "x"
    for col, x in enumerate(xs):  # the unity line, output = input
        row = ysub(x) // 2
        if classes[row][col] == " ":
            cells[row][col], classes[row][col] = "·", "z"
    if c is not None:
        ys = [ysub(comp_out(c, x)) for x in xs]
        for col in range(gw):
            lo, hi = (ys[col], ys[col]) if col == 0 else (min(ys[col - 1], ys[col]), max(ys[col - 1], ys[col]))
            for row in range(height):
                top, bot = lo <= 2 * row <= hi, lo <= 2 * row + 1 <= hi
                if top or bot:
                    cells[row][col] = "█" if top and bot else ("▀" if top else "▄")
                    classes[row][col] = "a"
    head = f"comp {fmt_comp(c) if c is not None else 'none'}"
    if c is not None and peaks:
        est = comp_estimate(c, peaks)
        if est and est[0] > 0:
            head += f"  works {est[0]:.0%} of the time, {est[1]:.1f} dB avg, {est[2]:.1f} dB most"
        elif est:
            head += "  never reaches the threshold on this track"
    rows = [(head, "", "head")]
    for row in range(height):
        label = "0" if row == 0 else ("-60" if row == height - 1 else ("-30" if row == height // 2 else ""))
        rows.append((f"{label:>{EQ_GUTTER - 1}} " + "".join(cells[row]),
                     " " * EQ_GUTTER + "".join(classes[row]), "graph"))
    axis = [" "] * gw
    for db, text in ((-60, "-60"), (-40, "-40"), (-20, "-20"), (0, "0")):
        col = round((db + 60) / 60 * (gw - 1))
        col = max(0, min(gw - len(text), col - len(text) // 2 if db else col - len(text) + 1))
        axis[col:col + len(text)] = list(text)
    rows.append((" " * EQ_GUTTER + "".join(axis), "", "axis"))
    return rows


class CompEffect(Effect):
    name = "comp"
    aliases = ("cp",)
    summary = "compressor: threshold, ratio, attack, release, knee, makeup"
    syntax = "-18 4:1 a10 r120 k6 m3"
    hint = "-18 4:1 a10 r120 k6 m3 | vocal"
    presets = COMP_PRESETS
    order = 20
    picture_width = (30, 64)
    legend = "· output = input   █ the compressor   ░ how often this track's peaks sit at that level"
    cheat = (
        " comp  cp TRACK -18 4:1 a10 r120 k6 m3  thr ratio a r k m",
        " comp  cp TRACK vocal|drums|glue..    presets (comp presets)",
    )
    help = (f"settings: {COMP_SYNTAX}",)

    def parse(self, text: str) -> dict:
        return parse_comp(text)

    def format(self, params: dict) -> str:
        return fmt_comp(params)

    def filters(self, ctx: FxContext, params: dict) -> list[str]:
        return [comp_filter(params)]

    def picture(self, ctx: FxContext, params: dict | None, width: int, height: int):
        return render_comp(params, width, height, ctx.peaks())
