"""Saturation for gout: drive the sound into a soft curve for warmth, grit or fuzz.

Install it with `gout addons examples`, which copies the examples into your addon folder, or
by hand:

    mkdir -p ~/.config/gout/addons && cp examples/addons/saturation.py ~/.config/gout/addons/

    gout sat 3 tape
    gout saturation 3 tanh d12 m70 t8k

Settings: the curve (tanh, atan, cubic, exp, alg, quintic, sin, erf or hard), drive in dB (d),
mix % (m: below 100 the clean sound is blended back in, parallel saturation), tone (t: a
low-pass in Hz after the curve, t0 for none, 8k and so on), output (o: a gain in dB, or oauto,
which keeps quiet passages at the level they came in), and the curve's shape (p, 0.01 to 3,
only for tanh, atan and alg).

ffmpeg's asoftclip gives output * threshold * curve(input / threshold), measured against every
curve below; with threshold and output at 1 that is curve(input), so the picture is exact.
The curve runs at four times the project rate (aresample up, clip, aresample down), which keeps
the harmonics it makes from folding back as aliasing. asoftclip's own oversample option is not
used: in ffmpeg 6.1 it loses about 6 dB and stops clipping. The resamplers add no delay, so the
clean path of a blend lines up without correction.
"""
import math
import re

from gout.fx import Effect, GUTTER

CURVES = {
    "hard":    lambda x, p: max(-1.0, min(1.0, x)),
    "tanh":    lambda x, p: math.tanh(x * p),
    "atan":    lambda x, p: 2 / math.pi * math.atan(x * p),
    "cubic":   lambda x, p: math.copysign(1.0, x) if abs(x) >= 1.5 else x - 0.1481 * x ** 3,
    "exp":     lambda x, p: 2 / (1 + math.exp(max(-700.0, min(700.0, -2 * x)))) - 1,
    "alg":     lambda x, p: x / math.sqrt(p + x * x),
    "quintic": lambda x, p: math.copysign(1.0, x) if abs(x) >= 1.25 else x - 0.08192 * x ** 5,
    "sin":     lambda x, p: math.copysign(1.0, x) if abs(x) >= math.pi / 2 else math.sin(x),
    "erf":     lambda x, p: math.erf(x),
}
DEFAULTS = {"curve": "tanh", "drive": 6.0, "mix": 100.0, "tone": 0.0, "output": None, "shape": 1.0}


def small_signal_gain(curve, shape):
    """The curve's slope at zero: how much a quiet sound is amplified by the curve itself."""
    h = 1e-6
    return CURVES[curve](h, shape) / h


def output_db(params):
    if params["output"] is not None:
        return params["output"]
    return -params["drive"] - 20 * math.log10(small_signal_gain(params["curve"], params["shape"]))


def quiet_gain(params):
    """What the whole effect does to a very quiet sound: 1 with oauto."""
    mix = params["mix"] / 100
    wet = 10 ** ((params["drive"] + output_db(params)) / 20) * small_signal_gain(params["curve"], params["shape"])
    return mix * wet + (1 - mix)


def transfer(params, x):
    """Output for an input sample x, everything included but the tone filter."""
    drive = 10 ** (params["drive"] / 20)
    wet = CURVES[params["curve"]](x * drive, params["shape"]) * 10 ** (output_db(params) / 20)
    mix = params["mix"] / 100
    return mix * wet + (1 - mix) * x


class Saturation(Effect):
    name = "saturation"
    aliases = ("sat",)
    summary = "saturation: a soft curve for warmth, grit or fuzz, with drive, blend and tone"
    syntax = "tanh d12 m70 t8k"
    hint = "tanh d12 m100 t8k | tape | tube | fuzz"
    presets = {
        "warm":   ("tanh d4 m60", "a little density, mostly clean"),
        "tape":   ("tanh d8 t14k", "rounded peaks and a softer top"),
        "tube":   ("atan d10 p1.5 m80", "thicker, with some of the clean sound kept"),
        "crunch": ("quintic d16 t9k", "audible grit"),
        "fuzz":   ("hard d30 t5k o-20", "squared off and dark, about as loud as it went in"),
    }
    order = 15              # after eq, before compression, when added by name
    picture_width = (30, 60)
    legend = "· output = input   █ the saturation, input across and output up, both -1 to 1"

    def parse(self, text):
        params = dict(DEFAULTS)
        for tok in text.lower().split():
            if tok in CURVES:
                params["curve"] = tok
            elif tok == "oauto":
                params["output"] = None
            elif re.fullmatch(r"o[+-]?\d+(\.\d+)?", tok):
                params["output"] = float(tok[1:])
            elif re.fullmatch(r"t\d+(\.\d+)?k?", tok):
                value = tok[1:]
                params["tone"] = float(value[:-1]) * 1000 if value.endswith("k") else float(value)
            elif re.fullmatch(r"[dmp]\d+(\.\d+)?", tok):
                params[{"d": "drive", "m": "mix", "p": "shape"}[tok[0]]] = float(tok[1:])
            else:
                raise ValueError(f"bad saturation setting {tok!r}; a line looks like  {self.syntax}"
                                 f"  (curves: {' '.join(CURVES)})")
        if not 0 <= params["drive"] <= 40:
            raise ValueError("drive must be between 0 and 40 dB")
        if not 0 <= params["mix"] <= 100:
            raise ValueError("mix is a percentage, 0 .. 100")
        if params["tone"] and not 500 <= params["tone"] <= 20000:
            raise ValueError("tone is a low-pass between 500 Hz and 20k, or t0 for none")
        if not 0.01 <= params["shape"] <= 3:
            raise ValueError("shape (p) must be between 0.01 and 3")
        if params["output"] is not None and not -40 <= params["output"] <= 12:
            raise ValueError("output must be between -40 and +12 dB")
        return params

    def format(self, p):
        """Curve and drive always; mix, tone, output and shape only when they are not the defaults."""
        words = [p["curve"], f"d{p['drive']:g}"]
        if p["mix"] != 100:
            words.append(f"m{p['mix']:g}")
        if p["tone"]:
            words.append(f"t{p['tone'] / 1000:g}k" if p["tone"] >= 1000 else f"t{p['tone']:g}")
        if p["output"] is not None:
            words.append(f"o{p['output']:g}")
        if p["shape"] != 1:
            words.append(f"p{p['shape']:g}")
        return " ".join(words)

    def graph(self, ctx, params, src, out, inputs):
        wet = [f"aresample={ctx.rate * 4}",
               f"volume={params['drive']:.3f}dB",
               f"asoftclip=type={params['curve']}:param={params['shape']:g}",
               f"volume={output_db(params):.3f}dB",
               f"aresample={ctx.rate}"]
        if params["tone"]:
            wet.append(f"lowpass=f={params['tone']:g}")
        mix = params["mix"] / 100
        if mix >= 1:
            return f"{src}{','.join(wet)}[{out}]"
        return (f"{src}asplit[{out}d][{out}w];"
                f"[{out}w]{','.join(wet)},volume={mix:.4f}[{out}ws];"
                f"[{out}d]volume={1 - mix:.4f}[{out}ds];"
                f"[{out}ds][{out}ws]amix=inputs=2:normalize=0:duration=longest[{out}]")

    def picture(self, ctx, params, width, height):
        gw = max(10, width - GUTTER)
        sub = 2 * height
        cells = [[" "] * gw for _ in range(height)]
        classes = [[" "] * gw for _ in range(height)]
        xs = [-1 + 2 * col / (gw - 1) for col in range(gw)]

        def ysub(v):
            return max(0, min(sub - 1, round((1 - max(-1.0, min(1.0, v))) / 2 * (sub - 1))))

        for col, x in enumerate(xs):
            row = ysub(x) // 2
            cells[row][col], classes[row][col] = "·", "z"
        head = f"saturation {self.format(params) if params else 'none'}"
        if params:
            ys = [ysub(transfer(params, x)) for x in xs]
            for col in range(gw):
                lo, hi = (ys[col], ys[col]) if col == 0 else (min(ys[col - 1], ys[col]), max(ys[col - 1], ys[col]))
                for row in range(height):
                    top, bot = lo <= 2 * row <= hi, lo <= 2 * row + 1 <= hi
                    if top or bot:
                        cells[row][col] = "█" if top and bot else ("▀" if top else "▄")
                        classes[row][col] = "a"
            peak = abs(transfer(params, 0.5))
            head += f"  a -6 dBFS peak comes out at {20 * math.log10(max(peak, 1e-9)):+.1f} dBFS"
            peaks = ctx.peaks()
            if peaks:
                levels = [v / 128 for v in peaks if v > 0]
                linear = quiet_gain(params)  # a peak is bent when the curve moves it more than 1 dB off that
                bent = sum(1 for x in levels
                           if abs(20 * math.log10(max(abs(transfer(params, x)), 1e-9) / (x * linear))) > 1)
                head += f"; bends {bent / max(1, len(levels)):.0%} of this track's peaks"
        rows = [(head, "", "head")]
        for row in range(height):
            label = "+1" if row == 0 else ("-1" if row == height - 1 else ("0" if row == height // 2 else ""))
            rows.append((f"{label:>{GUTTER - 1}} " + "".join(cells[row]), " " * GUTTER + "".join(classes[row]), "graph"))
        text = "-1" + " " * max(0, gw // 2 - 3) + "0" + " " * max(0, gw - gw // 2 - 3) + "+1"
        rows.append((" " * GUTTER + text[:gw], "", "axis"))
        return rows


def register(gout):
    gout.requires(1)  # the addon API this file is written for (docs/addons.md)
    gout.add_effect(Saturation())
