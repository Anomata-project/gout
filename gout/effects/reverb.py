"""Reverb: synthesised impulse responses, convolution graph, picture of the decay."""
from __future__ import annotations

import math
import random
import re
import sys
from array import array

from ..core import IR_DIR
from ..media import write_float_wav
from ..fx import Effect, FxContext, GUTTER as EQ_GUTTER


#
# A reverb line:  2.5s p20 d50 w25   (decay time to -60 dB, pre-delay ms, damping %, wet %).
# ffmpeg has no algorithmic reverb, so gout synthesises a stereo impulse response (early
# reflections, then decorrelated noise decaying at the set time, the high end decaying
# faster the more damping) and convolves a mono sum of the track with it (afir). The
# response is cached per setting under .gout/ir/ in the project.
REVERB_DEFAULTS = {"pre": 10.0, "damp": 40.0, "wet": 20.0}


REVERB_PRESETS = {
    "ambience":  ("0.4s p0 d30 w15", "a little air around it"),
    "room":      ("0.8s p5 d40 w20", "a small room"),
    "chamber":   ("1.4s p10 d45 w20", "a warm, dense chamber"),
    "plate":     ("1.8s p10 d15 w25", "bright and smooth, the vocal classic"),
    "hall":      ("2.6s p25 d50 w25", "a concert hall"),
    "cathedral": ("6s p40 d60 w30", "long and dark"),
}


REVERB_SYNTAX = "2.5s p20 d50 w25   (decay to -60 dB, pre-delay ms, damping %, wet %)"


REVERB_VERSION = 1  # bump when the synthesis changes, so cached responses are rebuilt


def parse_reverb(text: str) -> dict:
    tokens: list[str] = []
    for tok in text.split():
        if tok.lower() in REVERB_PRESETS:
            tokens += REVERB_PRESETS[tok.lower()][0].split()
        else:
            tokens.append(tok)
    r: dict = {"decay": None, **REVERB_DEFAULTS}
    for tok in tokens:
        t = tok.lower()
        if re.fullmatch(r"\d+(?:\.\d+)?(ms|s)", t):
            r["decay"] = float(t[:-2]) / 1000 if t.endswith("ms") else float(t[:-1])
        elif re.fullmatch(r"p\d+(?:\.\d+)?", t):
            r["pre"] = float(t[1:])
        elif re.fullmatch(r"d\d+(?:\.\d+)?", t):
            r["damp"] = float(t[1:])
        elif re.fullmatch(r"w\d+(?:\.\d+)?", t):
            r["wet"] = float(t[1:])
        else:
            raise ValueError(f"bad reverb setting {tok!r}; a line looks like  {REVERB_SYNTAX}  (or a preset: reverb presets)")
    if r["decay"] is None:
        raise ValueError(f"a reverb needs a decay time: {REVERB_SYNTAX}")
    if not 0.1 <= r["decay"] <= 8:
        raise ValueError("decay must be between 0.1 and 8 s")
    if not 0 <= r["pre"] <= 250:
        raise ValueError("pre-delay must be between 0 and 250 ms")
    if not 0 <= r["damp"] <= 100:
        raise ValueError("damping is a percentage, 0 .. 100")
    if not 0 <= r["wet"] <= 100:
        raise ValueError("wet is a percentage, 0 .. 100")
    return r


def fmt_reverb(r: dict) -> str:
    return f"{r['decay']:g}s p{r['pre']:g} d{r['damp']:g} w{r['wet']:g}"


def reverb_shape(r: dict) -> str:
    """The settings that shape the impulse response; wet is only a level, applied at convolution."""
    return f"{r['decay']:g}s_p{r['pre']:g}_d{r['damp']:g}"


def reverb_tail_ms(r: dict) -> int:
    return math.ceil(r["pre"] + r["decay"] * 1000)


def synth_ir(r: dict, rate: int) -> list[array]:
    """A stereo impulse response, unit energy per channel, reproducible for the same settings."""
    decay, damp = r["decay"], r["damp"] / 100
    pre = round(r["pre"] * rate / 1000)
    n_late = max(1, round(decay * rate))
    k_dark = math.exp(-6.9078 / (decay * rate))                    # -60 dB after `decay`
    k_bright = math.exp(-6.9078 / (max(0.05, decay * (1 - 0.85 * damp)) * rate))
    a = 1 - math.exp(-2 * math.pi * 2500 / rate)                   # one-pole low-pass at 2.5 kHz
    lp_gain = math.sqrt((2 - a) / a)                               # keeps its noise power at 1
    wb, wd = math.sqrt(1 - damp), math.sqrt(damp)
    build = max(1, round(0.012 * rate))                            # the late part swells in
    er_span = min(0.08, decay * 0.04 + 0.01)
    chans = []
    for c in range(2):
        rng = random.Random(f"gout-reverb-{REVERB_VERSION}-{reverb_shape(r)}-{rate}-{c}")
        gauss = rng.gauss
        out = array("f", bytes(4 * (pre + n_late)))
        env_b = env_d = 1.0
        y = 0.0
        energy = 0.0
        for i in range(n_late):
            x = gauss(0.0, 1.0)
            y += a * (x - y)
            v = wb * x * env_b + wd * y * lp_gain * env_d
            if i < build:
                v *= i / build
            out[pre + i] = v
            energy += v * v
            env_b *= k_bright
            env_d *= k_dark
        tap = math.sqrt(energy) * 0.12
        for _ in range(8):                                         # early reflections
            pos = pre + round(rng.uniform(0.003, er_span) * rate)
            if pos < len(out):
                out[pos] += rng.choice((-1.0, 1.0)) * rng.uniform(0.4, 1.0) * tap
        total = math.sqrt(sum(v * v for v in out)) or 1.0
        for i in range(len(out)):
            out[i] /= total
        chans.append(out)
    return chans


def impulse_path(project: "Project", r: dict) -> Path:
    """The cached impulse response for these settings, synthesised on first use."""
    key = f"v{REVERB_VERSION}-{reverb_shape(r)}-{project.rate}"
    path = project.root / IR_DIR / f"{key}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_float_wav(path, synth_ir(r, project.rate), project.rate)
    return path


def reverb_graph(project: "Project", r: dict, src: str, out: str, inputs: list[Path]) -> str:
    """Filtergraph from a stereo label to [out]: the dry signal plus a mono sum convolved with
    the stereo response. Appends the response file to `inputs`."""
    k = len(inputs)
    inputs.append(impulse_path(project, r))
    tail = reverb_tail_ms(r) / 1000
    return (f"{src}asplit[{out}d][{out}w];"
            f"[{out}w]pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,apad=pad_dur={tail:.3f}[{out}m];"
            f"[{k}:a]aformat=sample_rates={project.rate}:sample_fmts=fltp:channel_layouts=stereo[{out}i];"
            f"[{out}m][{out}i]afir=dry=1:wet={r['wet'] / 100:.4f}:gtype=none[{out}r];"
            f"[{out}d][{out}r]amix=inputs=2:normalize=0:duration=longest[{out}]")


def render_reverb(project: "Project", r: dict | None, width: int = 60, height: int = 6) -> list[tuple[str, str, str]]:
    """The impulse response's level over time, 0 to -60 dB, both channels' peak per column."""
    gw = max(12, width - EQ_GUTTER)
    cells = [[" "] * gw for _ in range(height)]
    classes = [[" "] * gw for _ in range(height)]
    total_ms = 1000.0
    if r is not None:
        path = impulse_path(project, r)
        data = array("f")
        data.frombytes(path.read_bytes()[44:])
        if sys.byteorder == "big":
            data.byteswap()
        frames = len(data) // 2
        total_ms = frames * 1000 / project.rate
        peak = max((abs(v) for v in data), default=1.0) or 1.0
        sub = 2 * height
        for col in range(gw):
            a = col * frames // gw
            b = max(a + 1, (col + 1) * frames // gw)
            level = max((abs(v) for v in data[2 * a:2 * b]), default=0.0) / peak
            db = 20 * math.log10(max(level, 1e-6))
            if db < -60:
                continue
            top_sub = round(-db / 60 * (sub - 1))
            for row in range(height):
                if 2 * row + 1 >= top_sub:
                    cells[row][col] = "█" if 2 * row >= top_sub else "▄"
                    classes[row][col] = "a"
    head = f"reverb {fmt_reverb(r) if r is not None else 'none'}"
    if r is not None:
        head += f"  rings {reverb_tail_ms(r) / 1000:.2f} s after the sound stops"
    rows = [(head, "", "head")]
    for row in range(height):
        label = "0" if row == 0 else ("-60" if row == height - 1 else ("-30" if row == height // 2 else ""))
        rows.append((f"{label:>{EQ_GUTTER - 1}} " + "".join(cells[row]), " " * EQ_GUTTER + "".join(classes[row]), "graph"))
    axis = [" "] * gw
    unit_s = total_ms >= 2000
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        ms = total_ms * frac
        text = f"{ms / 1000:.1f}s" if unit_s else f"{ms:.0f}ms"
        col = max(0, min(gw - len(text), round(frac * (gw - 1)) - (0 if frac == 0 else len(text) // 2)))
        if all(ch == " " for ch in axis[max(0, col - 1):col + len(text) + 1]):
            axis[col:col + len(text)] = list(text)
    rows.append((" " * EQ_GUTTER + "".join(axis), "", "axis"))
    return rows


class ReverbEffect(Effect):
    name = "reverb"
    aliases = ("rv", "verb")
    summary = "reverb: decay to -60 dB, pre-delay, damping, wet; convolution with a synthesised room"
    syntax = "2.5s p20 d50 w25"
    hint = "2.5s p20 d50 w25 | hall | plate"
    presets = REVERB_PRESETS
    order = 40
    picture_width = (40, 80)
    picture_height = 6
    cheat = (
        " reverb rv TRACK 2.5s p20 d50 w25     decay pre damp wet",
        " reverb rv TRACK room|plate|hall..    presets (rv presets)",
    )
    help = (f"settings: {REVERB_SYNTAX}",
            "the reverb of a mono sum, wide whatever comes before it; its tail counts toward the length")

    def parse(self, text: str) -> dict:
        return parse_reverb(text)

    def format(self, params: dict) -> str:
        return fmt_reverb(params)

    def graph(self, ctx: FxContext, params: dict, src: str, out: str, inputs: list) -> str:
        return reverb_graph(ctx, params, src, out, inputs)

    def tail_ms(self, ctx: FxContext, params: dict) -> int:
        return reverb_tail_ms(params)

    def picture(self, ctx: FxContext, params: dict | None, width: int, height: int):
        return render_reverb(ctx, params, width, height)
