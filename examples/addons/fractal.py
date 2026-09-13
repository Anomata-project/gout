"""Full-screen play for gout: the song as a Newton fractal of w = z³ + 7, moving with the music.

Install it by copying this file into your addon folder:

    mkdir -p ~/.config/gout/addons && cp examples/addons/fractal.py ~/.config/gout/addons/

In the ui, ctrl-space (or typing fractal) fills the terminal with it and plays from the playhead;
esc goes back to gout and the song keeps playing. Space plays and stops, the left and right arrows
move 5 s, up and down change the power (z² to z⁸), + and - the constant, c turns colours off.

Every character is a starting point z on the complex plane. Newton's method walks it towards a
root of w = z³ + 7 (a cube root of -7); the character says how many steps it took, its colour
which of the three roots it reached. Where the basins meet, the steps pile up into the fractal.
The music moves it: the bass bends the method (a relaxed Newton step, z - a·w/w' with a above 1)
and pushes the rotation, the overall level zooms in, hits and highs make it denser, and the mids
trade the basins' colours as they go by. It is also a template for writing your own screen: one
Screen subclass and a register() function.
"""
import cmath
import math
import time

from gout.screens import Screen

RAMP = " .:-=+*#%@"
SUPERSCRIPT = {2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸"}
LIMIT = 24           # Newton steps before a point counts as lost


def newton(width: int, height: int, power: int, constant: float, theta: float, zoom: float,
           relax: float, step: int = 1) -> tuple[list[int], list[int]]:
    """Steps to converge and the root reached, per cell, row by row. Every `step`-th column is
    computed and the ones between copy it."""
    cols = list(range(0, width, step))
    turn = cmath.exp(1j * theta) * zoom / (height / 2)
    zs = [complex((x - width / 2) * 0.5, height / 2 - y) * turn for y in range(height) for x in cols]
    count = len(zs)
    steps = [LIMIT] * count
    roots = [-1] * count
    c = complex(constant)
    base = cmath.phase(-c)
    tol = 1e-3 * max(1.0, abs(c))
    p1 = power - 1
    active = range(count)
    for n in range(LIMIT):
        still = []
        for i in active:
            z = zs[i]
            zp1 = z ** p1
            w = zp1 * z + c
            if abs(w) < tol:
                steps[i] = n
                roots[i] = round((cmath.phase(z) * power - base) / (2 * math.pi)) % power
                continue
            if zp1 == 0:
                continue
            zs[i] = z - relax * w / (power * zp1)
            still.append(i)
        active = still
        if not active:
            break
    if step == 1:
        return steps, roots
    wide_steps, wide_roots = [], []
    per_row = len(cols)
    for y in range(height):
        row_s, row_r = steps[y * per_row:(y + 1) * per_row], roots[y * per_row:(y + 1) * per_row]
        wide_steps += [row_s[x // step] for x in range(width)]
        wide_roots += [row_r[x // step] for x in range(width)]
    return wide_steps, wide_roots


class Fractal(Screen):
    name = "fractal"
    aliases = ("fz",)
    key = "ctrl-space"
    summary = "full-screen play: a Newton fractal of w = z³ + 7 moving with the music"
    fps = 20
    help = (("↑ ↓", "fractal: the power, z² to z⁸"), ("+ -", "fractal: the constant"),
            ("c", "fractal: colours on and off"))

    def __init__(self):
        self.power = 3
        self.constant = 7.0
        self.colours = True
        self.step = 1
        self.last = None     # (inputs, rows): a paused song draws the same frame without work

    def status(self, ctx):
        sign = "-" if self.constant < 0 else "+"
        return f"w = z{SUPERSCRIPT[self.power]} {sign} {abs(self.constant):g}"

    def key_pressed(self, ctx, key):
        if key == "up":
            self.power = min(8, self.power + 1)
        elif key == "down":
            self.power = max(2, self.power - 1)
        elif key in ("+", "="):
            self.constant += 2 if self.constant == -1 else 1   # 0 has one root that Newton crawls to
        elif key in ("-", "_"):
            self.constant -= 2 if self.constant == 1 else 1
        elif key == "c":
            self.colours = not self.colours
        else:
            return False
        return True

    def frame(self, ctx, width, height):
        t = ctx.position_ms / 1000
        low, high = ctx.band("low", 120), ctx.band("high", 80)
        level, onset = ctx.band("level", 400), ctx.band("onset", 60)
        radius = abs(self.constant) ** (1 / self.power)                 # where the roots are
        theta = 0.06 * t + 0.5 * ctx.travel("low") + 0.15 * ctx.travel("high")
        zoom = radius * 1.7 * (1.25 - 0.45 * level)
        relax = 1.0 + 0.3 * low
        lift = 1.5 * high + 2.0 * onset
        hue = int(ctx.travel("mid") / 3)
        inputs = (width, height, self.power, self.constant, self.colours, round(theta, 5), round(zoom, 5),
                  round(relax, 4), round(lift, 3), hue)
        if self.last is not None and self.last[0] == inputs:
            return self.last[1]
        began = time.monotonic()
        steps, roots = newton(width, height, self.power, self.constant, theta, zoom, relax, self.step)
        spent = time.monotonic() - began
        budget = 1 / self.fps
        if spent > 1.4 * budget and self.step < 3:      # a big terminal: fewer columns, same motion
            self.step += 1
        elif spent < 0.4 * budget and self.step > 1:
            self.step -= 1
        found = sorted(s for s in steps[::7] if s < LIMIT) or [0]
        fast, slow = found[0], found[int(len(found) * 0.97)]
        scale = (len(RAMP) - 1) / max(1, slow - fast)
        rows = []
        for y in range(height):
            text, classes = [], []
            for i in range(y * width, (y + 1) * width):
                s = steps[i]
                if s >= LIMIT:
                    text.append("@")
                    classes.append("m" if self.colours else " ")
                    continue
                text.append(RAMP[min(len(RAMP) - 1, max(0, int((s - fast) * scale + lift)))])
                classes.append(str((roots[i] + hue) % 10) if self.colours else " ")
            rows.append(("".join(text), "".join(classes)))
        self.last = (inputs, rows)
        return rows


def register(gout):
    gout.add_screen(Fractal())
