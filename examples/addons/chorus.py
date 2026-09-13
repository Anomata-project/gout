"""A stereo chorus for gout: copies of the sound, slightly delayed and gently wobbling in time.

Install it by copying this file into your addon folder:

    mkdir -p ~/.config/gout/addons && cp examples/addons/chorus.py ~/.config/gout/addons/

    gout chorus 3 classic
    gout chorus 3 v3 r0.5 d2 t25 m50 w100

Settings: voices per side (v), LFO rate in Hz (r), depth in ms (d), base delay in ms (t),
mix % (m: each voice as loud as the dry sound at 100), width % (w: at 0 both sides get the
same voices, at 100 they differ enough to spread wide).

Left and right go through ffmpeg's chorus separately with different delays and rates, which is
what makes it wide; that needs a filtergraph, so this effect overrides graph() instead of
filters(). ffmpeg's chorus mixes dry at in_gain*out_gain and each voice at decay*out_gain, and
its modulation is a sine that moves each voice's delay between t and t+d.
"""
import math
import re

from gout.fx import Effect, GUTTER

DEFAULTS = {"voices": 2, "rate": 0.4, "depth": 2.0, "delay": 25.0, "mix": 50.0, "width": 100.0}


def voices(params, side):
    """(delay ms, rate Hz, depth ms) for each voice on one side (0 left, 1 right)."""
    spread = params["width"] / 100 if side else 0.0
    out = []
    for i in range(params["voices"]):
        delay = params["delay"] * (1 + 0.35 * i) * (1 + 0.12 * spread)
        rate = params["rate"] * (1 + 0.27 * i) * (1 + 0.18 * spread)
        out.append((delay, rate, params["depth"]))
    return out


def chorus_filter(params, side):
    vs = voices(params, side)
    decay = params["mix"] / 100
    out_gain = 1 / math.sqrt(1 + len(vs) * decay * decay)  # keeps the level about where it was
    return (f"chorus=1:{out_gain:.4f}:"
            + "|".join(f"{d:.2f}" for d, _, _ in vs) + ":"
            + "|".join(f"{max(decay, 0.0001):.4f}" for _ in vs) + ":"
            + "|".join(f"{r:.3f}" for _, r, _ in vs) + ":"
            + "|".join(f"{depth:.2f}" for _, _, depth in vs))


class Chorus(Effect):
    name = "chorus"
    aliases = ("ch",)
    summary = "stereo chorus: voices, rate, depth, delay, mix and width"
    syntax = "v2 r0.4 d2 t25 m50 w100"
    hint = "v2 r0.4 d2 t25 m50 w100 | classic | wide"
    presets = {
        "subtle":  ("v2 r0.25 d1.5 t20 m30 w70", "a little movement and width"),
        "classic": ("v3 r0.5 d2 t25 m50 w100", "the eighties guitar sound"),
        "wide":    ("v4 r0.3 d3 t30 m60 w100", "thick and very wide"),
        "vibe":    ("v1 r4 d1 t8 m100 w40", "fast and wobbly, close to vibrato"),
    }
    order = 25              # after eq and comp, before delay and reverb
    picture_width = (40, 80)
    picture_height = 6
    legend = "█ left voices   · right voices: how far each voice's delay swings, in ms, over two seconds"
    cheat = (" chorus ch TRACK classic|v2 r0.4 m50  stereo chorus, addon",)

    def parse(self, text):
        params = dict(DEFAULTS)
        for tok in text.lower().split():
            m = re.fullmatch(r"([vrdtmw])(\d+(?:\.\d+)?)", tok)
            if not m:
                raise ValueError(f"bad chorus setting {tok!r}; a line looks like  {self.syntax}")
            key = {"v": "voices", "r": "rate", "d": "depth", "t": "delay", "m": "mix", "w": "width"}[m.group(1)]
            params[key] = int(float(m.group(2))) if key == "voices" else float(m.group(2))
        limits = {"voices": (1, 4), "rate": (0.05, 5), "depth": (0.1, 8), "delay": (5, 50),
                  "mix": (0, 100), "width": (0, 100)}
        for key, (lo, hi) in limits.items():
            if not lo <= params[key] <= hi:
                raise ValueError(f"chorus {key} must be between {lo:g} and {hi:g}")
        return params

    def format(self, p):
        return (f"v{p['voices']} r{p['rate']:g} d{p['depth']:g} t{p['delay']:g} "
                f"m{p['mix']:g} w{p['width']:g}")

    def graph(self, ctx, params, src, out, inputs):
        return (f"{src}channelsplit=channel_layout=stereo[{out}l][{out}r];"
                f"[{out}l]{chorus_filter(params, 0)}[{out}lc];"
                f"[{out}r]{chorus_filter(params, 1)}[{out}rc];"
                f"[{out}lc][{out}rc]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[{out}]")

    def tail_ms(self, ctx, params):
        return math.ceil(max(d + depth for side in (0, 1) for d, _, depth in voices(params, side)))

    def picture(self, ctx, params, width, height):
        """How far each voice's delay has moved over two seconds: the wobble you hear."""
        gw = max(10, width - GUTTER)
        cells = [[" "] * gw for _ in range(height)]
        classes = [[" "] * gw for _ in range(height)]
        head = f"chorus {self.format(params) if params else 'none'}"
        top = bottom = ""
        if params:
            lines = [(v, "·", "x") for v in voices(params, 1)] + [(v, "█", "a") for v in voices(params, 0)]
            for (delay, rate, depth), char, cls in lines:
                for col in range(gw):
                    t = 2.0 * col / max(1, gw - 1)
                    swing = (math.sin(2 * math.pi * rate * t) + 1) / 2   # 0 .. 1 of the depth
                    row = round((1 - swing) * (height - 1))
                    cells[row][col], classes[row][col] = char, cls
            delays = [d for side in (0, 1) for d, _, _ in voices(params, side)]
            head += f"  {params['voices']}+{params['voices']} voices at {min(delays):.0f}-{max(delays):.0f} ms"
            top, bottom = f"+{params['depth']:g}", "0"
        rows = [(head, "", "head")]
        for row in range(height):
            label = top if row == 0 else (bottom if row == height - 1 else "")
            rows.append((f"{label:>{GUTTER - 1}} " + "".join(cells[row]), " " * GUTTER + "".join(classes[row]), "graph"))
        rows.append((" " * GUTTER + "0" + " " * max(0, gw - 3) + "2s", "", "axis"))
        return rows


def register(gout):
    gout.add_effect(Chorus())
