"""Distortion for gout: a pedal in a line of text — overdrive, high gain, fuzz, or a bitcrusher.

Install it by copying this file into your addon folder:

    mkdir -p ~/.config/gout/addons && cp examples/addons/distortion.py ~/.config/gout/addons/

    gout dist 3 overdrive
    gout distortion 3 hard d36 a10 h300 t4k

Settings:
  soft | hard | crush   the clipper: tanh, a hard clip, or a bitcrusher followed by a clip
  d36                   drive in dB, 0 to 60
  a20                   asymmetry %: clips one side earlier, which adds the rougher even harmonics
  h700                  tight: a high-pass before the clipper so the low end does not turn to mush
  t4k                   tone: a low-pass after it (t0 for none)
  m100                  mix % with the clean sound
  o-6 | oauto           output: a gain, or (default) matched to this track's level
  b6 s8                 crush only: bits (1 to 16) and sample-rate reduction (1 to 64)

How it is built, and what was measured to build it (ffmpeg 6.1):
- The clipper runs at four times the project rate (aresample up and down) to keep aliasing
  down; asoftclip's own oversample option loses about 6 dB and stops clipping. Crush runs at the
  project rate, because the aliasing is the sound.
- Asymmetry adds a constant before the clipper and takes curve(constant) away after it, so
  silence stays silent. Clipping one side harder still leaves an offset while notes play (as in
  a real pedal), so a gentle 12 Hz high-pass after the clipper blocks it. dcshift cannot add it directly (it works on
  32-bit integers and clips anything past full scale), and aeval is slow, so the constant is
  generated silence shifted up (anullsrc, dcshift) and mixed in.
- acrusher rounds to steps of 1/(2^bits - 1) in its linear mode, with mix=0 for the fully crushed
  sound (its mix is backwards) and aa=0 for hard steps; its sample reduction applies regardless.
- oauto measures: it runs 30 seconds from the middle of the track's file through the same
  tight filter, drive, clipper and tone filter, compares the integrated loudness (LUFS, as the
  master's loudness target uses) before and after, and sets the output to give it back. The result is cached in the project's .gout/distortion/ per file and
  settings. A model from peak levels was tried first and missed by up to 12 dB on noisy material,
  because heavy clipping flattens everything to the same level whatever the crest factor was.
  Plain RMS was tried second and left bright, tight presets about 2 dB loud to the ear.
  Effects earlier in the chain and the clean blend of m are not part of the measurement. The master
  has no clean source to measure, so it models -12 dBFS sine peaks instead. o sets it by hand.
"""
import hashlib
import math
import re
import subprocess

from gout.fx import Effect, GUTTER

DEFAULTS = {"mode": "soft", "drive": 24.0, "asym": 0.0, "tight": 0.0, "tone": 0.0, "mix": 100.0,
            "output": None, "bits": 8.0, "samples": 1}
MODES = ("soft", "hard", "crush")
OVERSAMPLE = 4


def curve(mode, v):
    return math.tanh(v) if mode == "soft" else max(-1.0, min(1.0, v))


def bias(params):
    return params["asym"] / 100 * 0.5 if params["mode"] != "crush" else 0.0


def stage(params, x):
    """The clipper alone, for one sample: drive, bias, curve (or crush and clip), bias removed."""
    g = 10 ** (params["drive"] / 20)
    if params["mode"] == "crush":
        steps = 2 ** params["bits"] - 1
        return max(-1.0, min(1.0, math.floor(x * g * steps + 0.5) / steps))
    b = bias(params)
    return curve(params["mode"], x * g + b) - curve(params["mode"], b)


def sine_rms(params, peak):
    """RMS of a sine of this peak level after the clipper."""
    n = 48
    return math.sqrt(sum(stage(params, peak * math.sin(2 * math.pi * (k + 0.25) / n)) ** 2 for k in range(n)) / n)


def model_output_db(params):
    """The output gain that gives a -12 dBFS sine its RMS back: the fallback when nothing can be measured
    (the master, silence, a file too short for a loudness reading)."""
    rms_out = sine_rms(params, 0.25)
    if rms_out <= 0:
        return 0.0
    return max(-60.0, min(24.0, 20 * math.log10((0.25 / math.sqrt(2)) / rms_out)))


def clip_graph(p, src, out, rate):
    """src -> tight, drive, the clipper (at 4x unless crushing), back to the rate, offset blocked -> [out].
    Shared by the render and the level measurement, so they cannot disagree."""
    crush = p["mode"] == "crush"
    high = rate * (1 if crush else OVERSAMPLE)
    parts = []
    front = []
    if p["tight"]:
        front.append(f"highpass=f={p['tight']:g}:poles=2")
    if not crush:
        front.append(f"aresample={high}")
    front.append(f"volume={p['drive']:.3f}dB")
    parts.append(f"{src}{','.join(front)}[{out}a]")
    b = bias(p)
    if crush:
        parts.append(f"[{out}a]acrusher=bits={p['bits']:g}:samples={p['samples']}:mix=0:aa=0:mode=lin,"
                     f"asoftclip=type=hard[{out}e]")
    elif b:
        kind = "tanh" if p["mode"] == "soft" else "hard"
        parts.append(f"anullsrc=r={high}:cl=stereo,dcshift=shift={b:.6f}[{out}b1]")
        parts.append(f"anullsrc=r={high}:cl=stereo,dcshift=shift={-curve(p['mode'], b):.6f}[{out}b2]")
        parts.append(f"[{out}a][{out}b1]amix=inputs=2:normalize=0:duration=first,asoftclip=type={kind}[{out}c]")
        parts.append(f"[{out}c][{out}b2]amix=inputs=2:normalize=0:duration=first[{out}e]")
    else:
        parts.append(f"[{out}a]asoftclip=type={'tanh' if p['mode'] == 'soft' else 'hard'}[{out}e]")
    back = []
    if not crush:
        back.append(f"aresample={rate}")
    if b:
        back.append("highpass=f=12:poles=1")  # blocks the offset one-sided clipping leaves
    parts.append(f"[{out}e]{','.join(back) or 'anull'}[{out}]")
    return ";".join(parts)


def measured_output_db(ctx, p):
    """Run part of the track through the clipper and return the gain that restores its loudness."""
    source = ctx.source()
    try:
        st = source.stat()
    except OSError:
        return None
    shape = {k: p[k] for k in ("mode", "drive", "asym", "tight", "tone", "bits", "samples")}
    key = hashlib.sha1(repr((source.name, st.st_size, st.st_mtime, ctx.rate, ctx.chain_input(),
                             sorted(shape.items()))).encode()).hexdigest()
    cache = ctx.cache("distortion", key[:20] + ".txt")
    if cache.exists():
        try:
            return float(cache.read_text())
        except ValueError:
            pass
    length = ctx.track.get("length_ms", 0) / 1000
    start = max(0.0, length / 2 - 15)
    tone = f"lowpass=f={p['tone']:g}," if p["tone"] else ""
    graph = (f"[0:a]{','.join(ctx.chain_input())},asplit[mi][mw];"
             "[mi]ebur128=peak=none,anullsink;"
             + clip_graph(p, "[mw]", "mc", ctx.rate)
             + f";[mc]{tone}ebur128=peak=none[mo]")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", "30", "-i", str(source),
                             "-filter_complex", graph, "-map", "[mo]", "-f", "null", "-"], capture_output=True, text=True)
    found = re.findall(r"\[Parsed_ebur128_(\d+) @ [^\]]+\] Summary:\s+Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS",
                       result.stderr)
    if result.returncode != 0 or len(found) != 2:
        return None
    (_, before), (_, after) = sorted(found, key=lambda f: int(f[0]))
    if float(before) <= -69 or float(after) <= -69:  # silence, or too short to measure
        return None
    gain = max(-60.0, min(24.0, float(before) - float(after)))
    cache.write_text(f"{gain:.3f}")
    return gain


def output_db(ctx, params):
    if params["output"] is not None:
        return params["output"]
    if not ctx.is_master:
        measured = measured_output_db(ctx, params)
        if measured is not None:
            return measured
    return model_output_db(params)


class Distortion(Effect):
    name = "distortion"
    aliases = ("dist",)
    summary = "distortion: overdrive to fuzz, or a bitcrusher; tight, tone, asymmetry, level matched"
    syntax = "hard d36 a10 h300 t4k"
    hint = "hard d36 a10 h300 t4k | overdrive | fuzz | lofi"
    presets = {
        "overdrive": ("soft d24 a10 h720 t5k", "the mid-hump pedal: tight lows, warm top"),
        "crunch":    ("soft d30 a20 h400 t6k", "rhythm crunch with some bite"),
        "highgain":  ("hard d48 a5 h900 t6k", "tight and saturated, for heavy parts"),
        "fuzz":      ("hard d54 a40 t3k", "lopsided, woolly and dark"),
        "bitcrush":  ("crush d6 b6 s4", "six bits at a quarter of the rate"),
        "lofi":      ("crush d3 b8 s10 t5k m70", "old sampler, blended"),
        "broken":    ("crush d12 b3 s24", "barely holding together"),
    }
    order = 16                 # early in a chain: after eq, before dynamics and space
    picture_width = (30, 60)
    legend = "· output = input   █ the distortion, input across and output up, both -1 to 1"
    help = ("modes soft hard crush; d drive dB, a asymmetry %, h tight Hz, t tone Hz, m mix %,",
            "o output dB or oauto (level matched), b bits and s sample reduction for crush")

    def parse(self, text):
        p = dict(DEFAULTS)
        seen = set()
        for tok in text.lower().split():
            if tok in MODES:
                p["mode"] = tok
                continue
            if tok == "oauto":
                p["output"] = None
                continue
            m = re.fullmatch(r"([dahtmobs])([+-]?\d+(?:\.\d+)?)(k?)", tok)
            if not m or (m.group(3) and m.group(1) not in "ht") or (m.group(2)[0] in "+-" and m.group(1) != "o"):
                raise ValueError(f"bad distortion setting {tok!r}; a line looks like  {self.syntax}"
                                 "  (modes: soft hard crush)")
            key = {"d": "drive", "a": "asym", "h": "tight", "t": "tone", "m": "mix", "o": "output",
                   "b": "bits", "s": "samples"}[m.group(1)]
            value = float(m.group(2)) * (1000 if m.group(3) else 1)
            p[key] = int(value) if key == "samples" else value
            seen.add(key)
        crush = p["mode"] == "crush"
        if crush and "asym" in seen:
            raise ValueError("asymmetry is for soft and hard; a crush is symmetric")
        if not crush and seen & {"bits", "samples"}:
            raise ValueError("bits (b) and sample reduction (s) are for crush")
        checks = [("drive", 0, 60), ("asym", 0, 100), ("mix", 0, 100), ("bits", 1, 16), ("samples", 1, 64)]
        for key, lo, hi in checks:
            if not lo <= p[key] <= hi:
                raise ValueError(f"distortion {key} must be between {lo} and {hi}")
        for key, label in (("tight", "tight (h)"), ("tone", "tone (t)")):
            if p[key] and not 20 <= p[key] <= 20000:
                raise ValueError(f"{label} is a frequency between 20 and 20k, or 0 for none")
        if p["output"] is not None and not -60 <= p["output"] <= 24:
            raise ValueError("output must be between -60 and +24 dB")
        return p

    def format(self, p):
        def hz(f):
            return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"
        words = [p["mode"], f"d{p['drive']:g}"]
        if p["asym"]:
            words.append(f"a{p['asym']:g}")
        if p["tight"]:
            words.append(f"h{hz(p['tight'])}")
        if p["tone"]:
            words.append(f"t{hz(p['tone'])}")
        if p["mix"] != 100:
            words.append(f"m{p['mix']:g}")
        if p["output"] is not None:
            words.append(f"o{p['output']:g}")
        if p["mode"] == "crush":
            if p["bits"] != DEFAULTS["bits"]:
                words.append(f"b{p['bits']:g}")
            if p["samples"] != DEFAULTS["samples"]:
                words.append(f"s{p['samples']}")
        return " ".join(words)

    def graph(self, ctx, p, src, out, inputs):
        mix = p["mix"] / 100
        back = [f"volume={output_db(ctx, p):.3f}dB"]
        if p["tone"]:
            back.append(f"lowpass=f={p['tone']:g}")
        if mix >= 1:
            return clip_graph(p, src, f"{out}k", ctx.rate) + f";[{out}k]{','.join(back)}[{out}]"
        back.append(f"volume={mix:.4f}")
        return (f"{src}asplit[{out}d][{out}w];"
                + clip_graph(p, f"[{out}w]", f"{out}k", ctx.rate)
                + f";[{out}k]{','.join(back)}[{out}ws];"
                f"[{out}d]volume={1 - mix:.4f}[{out}ds];"
                f"[{out}ds][{out}ws]amix=inputs=2:normalize=0:duration=longest[{out}]")

    def picture(self, ctx, p, width, height):
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
        head = f"distortion {self.format(p) if p else 'none'}"
        if p:
            gain = 10 ** (output_db(ctx, p) / 20)
            mix = p["mix"] / 100
            ys = [ysub(mix * stage(p, x) * gain + (1 - mix) * x) for x in xs]
            for col in range(gw):
                lo, hi = (ys[col], ys[col]) if col == 0 else (min(ys[col - 1], ys[col]), max(ys[col - 1], ys[col]))
                for row in range(height):
                    top, bot = lo <= 2 * row <= hi, lo <= 2 * row + 1 <= hi
                    if top or bot:
                        cells[row][col] = "█" if top and bot else ("▀" if top else "▄")
                        classes[row][col] = "a"
            if p["output"] is None:
                basis = "a -12 dBFS source" if ctx.is_master or measured_output_db(ctx, p) is None else "this track"
                head += f"  output {output_db(ctx, p):+.1f} dB, measured on {basis}"
        rows = [(head, "", "head")]
        for row in range(height):
            label = "+1" if row == 0 else ("-1" if row == height - 1 else ("0" if row == height // 2 else ""))
            rows.append((f"{label:>{GUTTER - 1}} " + "".join(cells[row]), " " * GUTTER + "".join(classes[row]), "graph"))
        text = "-1" + " " * max(0, gw // 2 - 3) + "0" + " " * max(0, gw - gw // 2 - 3) + "+1"
        rows.append((" " * GUTTER + text[:gw], "", "axis"))
        return rows


def register(gout):
    gout.add_effect(Distortion())
