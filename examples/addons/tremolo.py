"""A tremolo for gout: the volume rises and falls at a steady rate.

Install it by copying this file into your addon folder:

    mkdir -p ~/.config/gout/addons && cp examples/addons/tremolo.py ~/.config/gout/addons/

Then `gout tremolo 3 5hz d50` works like any built-in effect: in `gout fx`, the parameter
sheet, gout.json, help and the cheat sheet. It doubles as the template for writing your own:
one Effect subclass and a register() function.
"""
import math
import re

from gout.fx import Effect, GUTTER


class Tremolo(Effect):
    name = "tremolo"
    aliases = ("trem",)
    summary = "tremolo: the volume rises and falls, rate in Hz and depth in %"
    syntax = "5hz d50"
    hint = "5hz d50 | slow | fast | chop"
    presets = {
        "slow": ("2hz d40", "a slow sway"),
        "fast": ("8hz d60", "a quick flutter"),
        "chop": ("12hz d100", "on and off"),
    }
    order = 35                 # after eq, comp and delay, before reverb, when added by name
    picture_width = (30, 70)
    picture_height = 6

    def parse(self, text):
        params = {"rate": 5.0, "depth": 50.0}
        for tok in text.lower().split():
            if re.fullmatch(r"\d+(\.\d+)?hz", tok):
                params["rate"] = float(tok[:-2])
            elif re.fullmatch(r"d\d+(\.\d+)?", tok):
                params["depth"] = float(tok[1:])
            else:
                raise ValueError(f"bad tremolo setting {tok!r}; a line looks like  5hz d50")
        if not 0.1 <= params["rate"] <= 20:
            raise ValueError("rate must be between 0.1 and 20 Hz")
        if not 0 <= params["depth"] <= 100:
            raise ValueError("depth is a percentage, 0 .. 100")
        return params

    def format(self, params):
        return f"{params['rate']:g}hz d{params['depth']:g}"

    def filters(self, ctx, params):
        return [f"tremolo=f={params['rate']:g}:d={params['depth'] / 100:.3f}"]

    def picture(self, ctx, params, width, height):
        """One second of the gain, full volume at the top and silence at the bottom."""
        gw = max(10, width - GUTTER)
        head = f"tremolo {self.format(params) if params else 'none'}"
        cells = [[" "] * gw for _ in range(height)]
        classes = [[" "] * gw for _ in range(height)]
        for col in range(gw):
            t = col / (gw - 1)
            if params:
                depth = params["depth"] / 100
                gain = 1 - depth * (0.5 - 0.5 * math.cos(2 * math.pi * params["rate"] * t))
            else:
                gain = 1.0
            level = round(gain * (height - 1))
            row = height - 1 - level
            cells[row][col], classes[row][col] = "█", "a"
        rows = [(head, "", "head")]
        for row in range(height):
            label = "1" if row == 0 else ("0" if row == height - 1 else "")
            rows.append((f"{label:>{GUTTER - 1}} " + "".join(cells[row]), " " * GUTTER + "".join(classes[row]), "graph"))
        axis = "0" + " " * (gw - 3) + "1s"
        rows.append((" " * GUTTER + axis, "", "axis"))
        return rows


def register(gout):
    gout.add_effect(Tremolo())
