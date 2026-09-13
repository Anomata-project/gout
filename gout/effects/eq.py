"""Equaliser: band syntax, ffmpeg filters, frequency response and its picture."""
from __future__ import annotations

import cmath
import math
import re


#
# A track's eq is one line of bands:  hp80  lp12k  hp80/24  +3@200  -4@2.5k/3  ls100:+2  hs8k:-3
# (cuts with a slope in dB per octave, peaks as gain@frequency/Q, shelves as frequency:gain).
EQ_SLOPES = {6: (1,), 12: (2,), 18: (2, 1), 24: (2, 2), 36: (2, 2, 2), 48: (2, 2, 2, 2)}


EQ_SHELF_Q = 0.707


EQ_MAX_BANDS = 16


EQ_SYNTAX = "hp80  lp12k  hp80/24  +3@200  -4@2.5k/3  ls100:+2  hs8k:-3"


EQ_PRESETS = {  # a preset name stands for these bands; they can be mixed with bands of your own
    "voice":   ("hp80 -3@250/1.5 +2@3k/1.2 hs10k:+1", "spoken word: no rumble, less box, more presence"),
    "podcast": ("hp80 -2@300/1.5 +2@2.5k/1.2 lp16k", "voice, a touch softer, nothing above 16 kHz"),
    "warm":    ("ls200:+2 hs6k:-2", "a little more low end, a little less top"),
    "air":     ("hs10k:+3", "sheen above 10 kHz"),
    "bright":  ("hs4k:+3", "more top from 4 kHz up"),
    "mud":     ("-4@250/1.2", "takes the mud out around 250 Hz"),
    "clean":   ("hp40", "just the rumble under 40 Hz gone"),
    "phone":   ("hp300 lp3.4k", "the telephone effect"),
    "bass":    ("hp30 ls100:+3", "bass instruments: subsonics gone, body up"),
    "kick":    ("hp40 +3@60/1.5 -3@400/1.5 +2@4k/1.5", "kick drum: thump, less cardboard, click"),
    "guitar":  ("hp100 -2@300/1.5 +2@2.5k/1.5", "guitars sit better: less low mud, more bite"),
    "flat":    ("", "no eq at all"),
}


def parse_hz(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(k?)", text.lower())
    if not m:
        raise ValueError(f"bad frequency {text!r} (80, 2.5k, 12k)")
    f = float(m.group(1)) * (1000 if m.group(2) else 1)
    if not 10 <= f <= 22000:
        raise ValueError(f"frequency {text} is outside 10 Hz .. 22 kHz")
    return f


def fmt_hz(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


def parse_band(token: str) -> dict:
    t = token.lower()
    m = re.fullmatch(r"(hp|lp)(\d+(?:\.\d+)?k?)(?:/(\d+))?", t)
    if m:
        slope = int(m.group(3) or 12)
        if slope not in EQ_SLOPES:
            raise ValueError(f"slope {slope} in {token!r}: use 6, 12, 18, 24, 36 or 48 dB per octave")
        return {"type": m.group(1), "f": parse_hz(m.group(2)), "slope": slope}
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)@(\d+(?:\.\d+)?k?)(?:/(\d+(?:\.\d+)?))?", t)
    if m:
        g, q = float(m.group(1)), float(m.group(3) or 1)
        if not -24 <= g <= 24:
            raise ValueError(f"gain {g:+g} in {token!r}: keep it within ±24 dB")
        if not 0.1 <= q <= 20:
            raise ValueError(f"Q {q:g} in {token!r}: use 0.1 .. 20")
        return {"type": "peak", "f": parse_hz(m.group(2)), "g": g, "q": q}
    m = re.fullmatch(r"(ls|hs)(\d+(?:\.\d+)?k?):([+-]?\d+(?:\.\d+)?)", t)
    if m:
        g = float(m.group(3))
        if not -24 <= g <= 24:
            raise ValueError(f"gain {g:+g} in {token!r}: keep it within ±24 dB")
        return {"type": m.group(1), "f": parse_hz(m.group(2)), "g": g}
    raise ValueError(f"bad band {token!r}; bands look like  {EQ_SYNTAX}  (or a preset: eq presets)")


def fmt_band(b: dict) -> str:
    if b["type"] in ("hp", "lp"):
        return f"{b['type']}{fmt_hz(b['f'])}" + (f"/{b['slope']}" if b["slope"] != 12 else "")
    if b["type"] == "peak":
        return f"{b['g']:+g}@{fmt_hz(b['f'])}" + (f"/{b['q']:g}" if b["q"] != 1 else "")
    return f"{b['type']}{fmt_hz(b['f'])}:{b['g']:+g}"


def parse_eq(text: str) -> list[dict]:
    """Bands from a line of tokens, in a fixed order: hp, lp, then the rest as written.
    A preset name in the line stands for its bands."""
    tokens: list[str] = []
    for tok in text.split():
        if tok.lower() in EQ_PRESETS:
            tokens += EQ_PRESETS[tok.lower()][0].split()
        else:
            tokens.append(tok)
    bands = [parse_band(tok) for tok in tokens]
    cuts = {}
    rest = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            cuts[b["type"]] = b  # the last hp or lp wins
        else:
            rest.append(b)
    if len(rest) > EQ_MAX_BANDS:
        raise ValueError(f"more than {EQ_MAX_BANDS} bands")
    return [cuts[k] for k in ("hp", "lp") if k in cuts] + rest


def fmt_eq(bands: list[dict]) -> str:
    return " ".join(fmt_band(b) for b in bands)


def eq_filters(bands: list[dict]) -> list[str]:
    out: list[str] = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            name = "highpass" if b["type"] == "hp" else "lowpass"
            out += [f"{name}=f={b['f']:g}:poles={poles}" for poles in EQ_SLOPES[b["slope"]]]
        elif b["type"] == "peak":
            out.append(f"equalizer=f={b['f']:g}:t=q:w={b['q']:g}:g={b['g']:g}")
        else:
            name = "lowshelf" if b["type"] == "ls" else "highshelf"
            out.append(f"{name}=f={b['f']:g}:t=q:w={EQ_SHELF_Q}:g={b['g']:g}")
    return out


def track_eq(t: dict) -> list[dict]:
    """The stored bands of a track, if its eq is on."""
    if not t.get("eq") or not t.get("eq_on", 1):
        return []
    try:
        return parse_eq(t["eq"])
    except ValueError:
        return []


def biquad(kind: str, f: float, fs: float, q: float = EQ_SHELF_Q, g: float = 0.0) -> tuple:
    """RBJ cookbook coefficients, the same family ffmpeg's biquads use."""
    w0 = 2 * math.pi * min(f, fs * 0.499) / fs
    cw, sw = math.cos(w0), math.sin(w0)
    if kind == "lp1":
        a1 = -math.exp(-w0)
        return (1 + a1, 0.0, 0.0, 1.0, a1, 0.0)
    if kind == "hp1":
        a1 = -math.exp(-w0)
        b0 = (1 - a1) / 2
        return (b0, -b0, 0.0, 1.0, a1, 0.0)
    alpha = sw / (2 * q)
    if kind == "lp2":
        return ((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if kind == "hp2":
        return ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    A = 10 ** (g / 40)
    if kind == "peak":
        return (1 + alpha * A, -2 * cw, 1 - alpha * A, 1 + alpha / A, -2 * cw, 1 - alpha / A)
    r = 2 * math.sqrt(A) * alpha
    if kind == "ls":
        return (A * ((A + 1) - (A - 1) * cw + r), 2 * A * ((A - 1) - (A + 1) * cw),
                A * ((A + 1) - (A - 1) * cw - r), (A + 1) + (A - 1) * cw + r,
                -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - r)
    return (A * ((A + 1) + (A - 1) * cw + r), -2 * A * ((A - 1) + (A + 1) * cw),  # hs
            A * ((A + 1) + (A - 1) * cw - r), (A + 1) - (A - 1) * cw + r,
            2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - r)


def eq_sections(bands: list[dict], fs: float) -> list[tuple]:
    out = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            for poles in EQ_SLOPES[b["slope"]]:
                out.append(biquad(f"{b['type']}{poles}", b["f"], fs))
        elif b["type"] == "peak":
            out.append(biquad("peak", b["f"], fs, b["q"], b["g"]))
        else:
            out.append(biquad(b["type"], b["f"], fs, EQ_SHELF_Q, b["g"]))
    return out


def eq_response(bands: list[dict], fs: float, freqs: list[float]) -> list[float]:
    """Gain in dB of the whole eq at each frequency."""
    sections = eq_sections(bands, fs)
    out = []
    for f in freqs:
        z1 = cmath.exp(-1j * 2 * math.pi * min(f, fs * 0.499) / fs)
        z2 = z1 * z1
        db = 0.0
        for b0, b1, b2, a0, a1, a2 in sections:
            h = abs((b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2))
            db += 20 * math.log10(h) if h > 1e-9 else -180
        out.append(db)
    return out


EQ_GUTTER = 4


EQ_TICKS = ((20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"),
            (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k"))


def render_eq(project: "Project", t: dict, width: int, height: int = 8,
              spectrum: bytes | None = None) -> list[tuple[str, str, str]]:
    """Rows of (text, classes, kind) plotting a track's eq curve from 20 Hz to 20 kHz.

    kind is head, graph or axis. Classes: a the curve, z the 0 dB line, x the spectrum.
    Half blocks give two levels per row; the dB range fits the curve, at least ±6.
    """
    gw = max(12, width - EQ_GUTTER)
    freqs = [20 * (1000 ** (c / (gw - 1))) for c in range(gw)]
    try:
        bands = parse_eq(t["eq"]) if t["eq"] else []
    except ValueError:
        bands = []
    curve = eq_response(bands, project.rate, freqs)
    # the range follows the boosts and cuts of peaks and shelves; cut slopes run off the bottom
    loudest = max([abs(b["g"]) for b in bands if b["type"] in ("peak", "ls", "hs")] + [0.0])
    span = max(12, min(24, math.ceil((loudest + 1.5) / 6) * 6))
    sub = 2 * height

    def ysub(db: float) -> int:
        db = max(-span, min(span, db))
        return max(0, min(sub - 1, round((span - db) / (2 * span) * (sub - 1))))

    cells = [[" "] * gw for _ in range(height)]
    classes = [[" "] * gw for _ in range(height)]
    zero_row = ysub(0) // 2
    if spectrum:  # background: one bar per column, dim
        for c in range(gw):
            level = spectrum[min(len(spectrum) - 1, c * len(spectrum) // gw)] / 255  # 0..1 of the range
            top_sub = round((1 - level) * (sub - 1))
            for row in range(height):
                if 2 * row >= top_sub:
                    cells[row][c], classes[row][c] = "░", "x"
    for c in range(gw):
        if classes[zero_row][c] == " ":
            cells[zero_row][c], classes[zero_row][c] = "─", "z"
    ys = [ysub(v) for v in curve]
    for c in range(gw):
        lo, hi = (ys[c], ys[c]) if c == 0 else (min(ys[c - 1], ys[c]), max(ys[c - 1], ys[c]))
        for row in range(height):
            top, bot = lo <= 2 * row <= hi, lo <= 2 * row + 1 <= hi
            if top or bot:
                cells[row][c] = "█" if top and bot else ("▀" if top else "▄")
                classes[row][c] = "a"
    state = "" if not t["eq"] else ("  (off)" if not t["eq_on"] else "")
    rows = [(f"eq   {t['n']:>2} {t['name'][:9]:<9} {t['eq'] or 'flat'}{state}"
             + ("  ░ spectrum" if spectrum else "")[:width], "", "head")]
    for row in range(height):
        label = f"{span:+d}" if row == 0 else (f"{-span:+d}" if row == height - 1 else ("0" if row == zero_row else ""))
        rows.append((f"{label:>{EQ_GUTTER - 1}} " + "".join(cells[row]),
                     " " * EQ_GUTTER + "".join(classes[row]), "graph"))
    axis = [" "] * gw
    last = -2
    for f, text in EQ_TICKS:
        c = round(math.log(f / 20) / math.log(1000) * (gw - 1))
        c = max(0, min(gw - len(text), c - len(text) // 2))
        if c > last + 1:
            axis[c:c + len(text)] = list(text)
            last = c + len(text) - 1
    rows.append((" " * EQ_GUTTER + "".join(axis), "", "axis"))
    return rows
