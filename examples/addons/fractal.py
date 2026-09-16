"""Full-screen play for gout: the song as a Newton fractal, moving with the music.

Install it with `gout addons examples`, which copies the examples into your addon folder, or
by hand:

    mkdir -p ~/.config/gout/addons && cp examples/addons/fractal.py ~/.config/gout/addons/

In the ui, ctrl-space fills the screen with it and plays from the playhead; esc goes back to gout
and the song keeps playing. In GNOME Terminal the window goes fullscreen too (run gout in a
window of its own: a window with several tabs keeps its tab bar). The status line hides after a
few seconds; any key brings it back.

Choosing the formula:
  - in the fractal, 1 .. 9 and 0 pick the first ten presets, up and down step through all of them;
    the choice is remembered
  - at the gout prompt, fractal 5, fractal rings, or a formula of your own: fractal z^5 - 3z + 1
  - fractal presets lists them
  - ~/.config/gout/fractal.json holds the presets (written the first time): change a formula, add
    your own, set "preset" to the one to start with, "fullscreen" and "status_seconds"
Formulas take z, numbers (3i for imaginary), + - * / ^, and sin cos tan sinh cosh tanh exp log sqrt.
The file is read each time the fractal opens.

Every character is a starting point z on the complex plane. Newton's method walks it towards a
root of the formula; the character says how many steps it took, its colour which root it reached,
and where the basins meet the steps pile up into the fractal. Points that never settle stay dark.
The music moves it: the bass bends the method (a relaxed Newton step, z - a·w/w' with a above 1)
and pushes the rotation, the overall level zooms in, hits and highs make it denser, and the mids
trade the basins' colours as they go by. It is also a template for writing your own screen: one
Screen subclass and a register() function.

gout video fractal 3 1 0 (or all) draws it into a video, changing preset every 10 s on a drum hit:
choices() and pick() are what the video uses, and ctx.offline says a video is being drawn.

The zoom (zoom at the prompt, gout video zoom 3 1 0 or all) takes the same presets and dives in
for as long as the song plays: towards a point Newton's method keeps coming back to every 2 or 3
steps. Around such a point the picture repeats, a few times smaller each time, so once the view is
100000 times deeper it quietly goes back up one repeat and carries on: the numbers never run out.
Time pushes the zoom, the level and the bass push it harder, and in silence it only drifts. The
bass does not bend the method here (that would move the edge away from the point). 1 .. 9 0 and
up and down dive into another preset, + and - go nearer and further; zoom_preset in fractal.json
remembers its choice.
"""
import cmath
import json
import math
import time

from gout.formula import FormulaError, parse
from gout.screens import Screen, config_file

RAMP = " .:-=+*#%@"
LIMIT = 24                     # Newton steps before a point counts as never settling
KEYS = "1234567890"
FILE = "fractal.json"

PRESETS = [
    {"name": "seven", "formula": "z^3 + 7", "about": "three basins"},
    {"name": "classic", "formula": "z^3 - 1", "about": "the classic Newton fractal"},
    {"name": "four", "formula": "z^4 - 1", "about": "four-fold"},
    {"name": "star", "formula": "z^5 - 1", "about": "a five-pointed star"},
    {"name": "rings", "formula": "z^8 + 15z^4 - 16", "about": "two rings of four roots"},
    {"name": "islands", "formula": "z^3 - 2z + 2", "about": "small dark islands where Newton never settles",
     "zoom": 1.3, "center": "0.4"},
    {"name": "twins", "formula": "z^6 + z^3 - 1", "about": "two rings of three roots"},
    {"name": "lopsided", "formula": "z^5 - z - 1", "about": "mirrored only top to bottom"},
    {"name": "waves", "formula": "sin(z)", "about": "an endless row of roots", "zoom": 5},
    {"name": "ladder", "formula": "cosh(z) - 2", "about": "two mirrored columns of roots", "zoom": 6},
]

DOCUMENT = {
    "about": ("Presets for gout's fractal screen. The first ten are keys 1..9 and 0 in the fractal; "
              "up and down step through all. formula: in z, with + - * / ^, numbers (3i imaginary), "
              "sin cos tan sinh cosh tanh exp log sqrt. zoom: how much of the plane shows, top to "
              "bottom half; center: the middle, like 0.7 or 1-2i. Both optional."),
    "preset": "seven",
    "fullscreen": True,
    "status_seconds": 3,
    "presets": PRESETS,
}


def cell_points(width, height, center, half, theta):
    """The complex point under every character, row by row: half is the view's half height,
    characters count as twice as tall as wide, the view turned by theta."""
    turn = cmath.exp(1j * theta) * half / (height / 2)
    return [center + complex((x - width / 2) * 0.5, height / 2 - y) * turn for y in range(height) for x in range(width)]


class Fractal(Screen):
    name = "fractal"
    aliases = ("fz",)
    key = "ctrl-space"
    summary = "full-screen play: a Newton fractal moving with the music (fractal presets, fractal.json)"
    fps = 20
    fullscreen = True
    remembered = "preset"          # the key in fractal.json that holds the last choice
    help = (("1 .. 9 0", "fractal: pick one of the first ten presets"),
            ("↑ ↓", "fractal: the previous or next preset"),
            ("+ -", "fractal: zoom in and out"), ("c", "fractal: colours on and off"))

    def __init__(self):
        self.presets = []          # [{"name", "formula" (Formula), "about", "zoom", "center"}]
        self.current = 0
        self.custom = None         # a formula typed at the prompt, until another is picked or it reopens
        self.colours = True
        self.zoom_factor = 1.0
        self.step = 1
        self.last = None           # (inputs, rows): a paused song draws the same frame without work
        self.framing = None        # (center, half) for the current formula
        self.roots = []            # roots found so far: their order is their colour
        self.root_cells = {}       # rounded settle point -> root index

    # ---- the file

    def load(self) -> list[str]:
        """Read fractal.json (writing it the first time). Lines worth telling the user."""
        path, notes = config_file(FILE), []
        doc = None
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(DOCUMENT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                notes.append(f"fractal: wrote the presets to {path}")
            except OSError as exc:
                notes.append(f"fractal: could not write {path}: {exc}")
        try:
            doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else DOCUMENT
            if not isinstance(doc, dict):
                raise ValueError("it is not a json object")
        except (OSError, ValueError) as exc:
            notes.append(f"fractal: {path.name} unreadable ({exc}); using the built-in presets")
            doc = DOCUMENT
        presets = []
        for i, entry in enumerate(doc.get("presets") if isinstance(doc.get("presets"), list) else PRESETS):
            if not isinstance(entry, dict):
                continue
            label = entry.get("name") or f"preset {i + 1}"
            try:
                formula = parse(entry.get("formula", ""))
                zoom = float(entry["zoom"]) if "zoom" in entry else None
                center = complex(str(entry.get("center", "0")).replace(" ", "").replace("i", "j")) if "center" in entry else None
            except (FormulaError, ValueError, TypeError) as exc:
                notes.append(f"fractal: {path.name} {label}: {exc}")
                continue
            presets.append({"name": str(label), "formula": formula, "about": str(entry.get("about", "")),
                            "zoom": zoom, "center": center})
        if not presets:
            notes.append(f"fractal: no usable presets in {path.name}; using the built-in ones")
            presets = [{"name": p["name"], "formula": parse(p["formula"]), "about": p["about"],
                        "zoom": p.get("zoom"), "center": complex(p["center"]) if "center" in p else None}
                       for p in PRESETS]
        wanted = str(doc.get(self.remembered, doc.get("preset", "")))
        previous = self.presets[self.current]["name"] if self.presets else None
        self.presets = presets
        self.fullscreen = doc.get("fullscreen", True) is not False
        try:
            self.status_seconds = max(0.0, float(doc.get("status_seconds", 3)))
        except (TypeError, ValueError):
            self.status_seconds = 3.0
        self.choose(self.find(previous or wanted) or 0, remember=False)
        return notes

    def remember(self) -> None:
        path = config_file(FILE)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get(self.remembered) != self.presets[self.current]["name"]:
                doc[self.remembered] = self.presets[self.current]["name"]
                tmp = path.with_suffix(".part")
                tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                tmp.replace(path)
        except (OSError, ValueError):
            pass  # a file the user broke stays as it is

    # ---- choosing

    def find(self, word):
        if word is None:
            return None
        word = str(word).strip().lower()
        if len(word) == 1 and word in KEYS[:len(self.presets)]:
            return KEYS.index(word)
        return next((i for i, p in enumerate(self.presets) if p["name"].lower() == word), None)

    def choose(self, index, remember=True):
        self.current, self.custom = index % len(self.presets), None
        self.reset()
        if remember:
            self.remember()

    def reset(self):
        self.framing, self.roots, self.root_cells, self.last = None, [], {}, None
        self.zoom_factor = 1.0

    def preset(self):
        if self.custom is not None:
            return self.custom
        return self.presets[self.current]

    def command(self, ctx, words):
        lines = self.load()
        text = " ".join(words).strip()
        if not text:
            return lines, True
        if text.lower() in ("presets", "list"):
            for i, p in enumerate(self.presets):
                key = KEYS[i] if i < len(KEYS) else " "
                mark = "▸" if i == self.current else " "
                lines.append(f" {mark}{key} {p['name']:<10} {p['formula'].pretty:<22} {p['about']}")
            lines.append(f"   {self.name} N, {self.name} NAME, or {self.name} FORMULA; the list lives in {config_file(FILE)}")
            return lines, False
        found = self.find(text)
        if found is not None:
            self.choose(found)
            return lines, True
        formula = parse(text)  # FormulaError is a ValueError: the ui shows it and does not open
        self.custom = {"name": "custom", "formula": formula, "about": "", "zoom": None, "center": None}
        self.reset()
        lines.append(f"fractal: w = {formula.pretty}; add it to {FILE} to keep it")
        return lines, True

    def choices(self):
        """The preset names, for gout video fractal all."""
        if not self.presets:
            self.load()
        return [p["name"] for p in self.presets]

    def pick(self, ctx, word):
        """A preset by key or name, or a formula, without remembering it (for a video)."""
        if not self.presets:
            self.load()
        found = self.find(word)
        if found is not None:
            self.choose(found, remember=False)
            return
        self.custom = {"name": "custom", "formula": parse(word), "about": "", "zoom": None, "center": None}
        self.reset()

    def status(self, ctx):
        p = self.preset()
        label = "custom" if self.custom is not None else f"{KEYS[self.current] if self.current < 10 else ''} {p['name']}".strip()
        return f"{label}  w = {p['formula'].pretty}"

    def key_pressed(self, ctx, key):
        if key in KEYS and KEYS.index(key) < len(self.presets):
            self.choose(KEYS.index(key))
        elif key in ("up", "down"):
            self.choose(self.current + (1 if key == "down" else -1))
        elif key in ("+", "="):
            self.zoom_factor /= 1.25
            self.last = None
        elif key in ("-", "_"):
            self.zoom_factor *= 1.25
            self.last = None
        elif key == "c":
            self.colours = not self.colours
            self.last = None
        else:
            return False
        return True

    # ---- drawing

    def root_of(self, z, tolerance):
        """The colour index of the root z settled on. The roots a first look finds come first, in a
        fixed order; one found later takes a colour from where it is, so the colours are the same
        whatever order frames are drawn in (a video draws them in several processes at once)."""
        cell = (round(z.real / tolerance), round(z.imag / tolerance))
        index = self.root_cells.get(cell)
        if index is None:
            index = next((i for i, r in enumerate(self.roots) if abs(r - z) < 5 * tolerance), None)
            if index is None:
                spot = (round(z.real / (50 * tolerance)), round(z.imag / (50 * tolerance)))
                index = len(self.roots) + (spot[0] * 7 + spot[1] * 13) % 10
            self.root_cells[cell] = index
        return index

    def seed_roots(self, p):
        """The roots a coarse look over the view finds, ordered by angle around the centre, then distance."""
        center, half = self.framing
        _, finals = p["formula"].newton()(cell_points(64, 32, center, half * 1.5, 0.0), 40, 1.0, half * 2e-4)
        found = []
        for z in finals:
            if z is not None and all(abs(z - r) >= 5 * half * 1e-3 for r in found):
                found.append(z)
        found.sort(key=lambda z: (round(cmath.phase(z - center), 3), round(abs(z - center), 3)))
        self.roots, self.root_cells = found, {}

    def frame_for(self, p):
        """Where to look: the preset's own center and zoom, or around the roots a first look finds."""
        if self.framing is None:
            center = p["center"] if p["center"] is not None else 0j
            if p["zoom"] is not None:
                self.framing = (center, p["zoom"])
            else:
                points = cell_points(48, 24, center, 4.0, 0.0)
                _, finals = p["formula"].newton()(points, 40, 1.0, 1e-7)
                found = []
                for z in finals:
                    if z is not None and abs(z - center) < 6 and all(abs(z - r) > 1e-3 for r in found):
                        found.append(z)
                if p["center"] is None and found:
                    center = sum(found) / len(found)
                spread = max((abs(r - center) for r in found), default=1.0)
                self.framing = (center, max(0.6, spread * 1.7))
            self.seed_roots(p)
        return self.framing

    def frame(self, ctx, width, height):
        p = self.preset()
        t = ctx.position_ms / 1000
        low, high = ctx.band("low", 120), ctx.band("high", 80)
        level, onset = ctx.band("level", 400), ctx.band("onset", 60)
        center, half = self.frame_for(p)
        theta = 0.06 * t + 0.5 * ctx.travel("low") + 0.15 * ctx.travel("high")
        half = half * self.zoom_factor * (1.25 - 0.45 * level)
        relax = 1.0 + 0.3 * low
        lift = 1.5 * high + 2.0 * onset
        hue = int(ctx.travel("mid") / 3)
        inputs = (width, height, id(p), self.colours, round(theta, 5), round(half, 6), round(relax, 4),
                  round(lift, 3), hue)
        if self.last is not None and self.last[0] == inputs:
            return self.last[1]
        began = time.monotonic()
        step = self.step  # on a big screen every step-th column is worked out and stretched back out
        cols = -(-width // step)
        turn = cmath.exp(1j * theta) * half / (height / 2)
        points = [center + complex((x * step - width / 2) * 0.5, height / 2 - y) * turn
                  for y in range(height) for x in range(cols)]
        steps, finals = p["formula"].newton()(points, LIMIT, relax, half * 2e-4)
        spent = time.monotonic() - began
        budget = 1 / self.fps
        if getattr(ctx, "offline", False):
            self.step = 1  # a video waits for every column; the same moment gives the same picture
        elif spent > 1.4 * budget and self.step < 3:
            self.step += 1
        elif spent < 0.4 * budget and self.step > 1:
            self.step -= 1
        settled = sorted(s for s in steps[::7] if s < LIMIT) or [0]
        fast, slow = settled[0], settled[int(len(settled) * 0.97)]
        scale = (len(RAMP) - 1) / max(1, slow - fast)
        tolerance = self.framing[1] * 1e-3  # fixed per formula, so a root keeps its colour
        rows = []
        for y in range(height):
            text, classes = [], []
            base = y * cols
            for x in range(width):
                i = base + x // step
                s = steps[i]
                if s >= LIMIT:
                    text.append(" ")
                    classes.append(" ")
                    continue
                text.append(RAMP[min(len(RAMP) - 1, max(1, int((s - fast) * scale + lift)))])  # blank is for never
                classes.append(str((self.root_of(finals[i], tolerance) + hue) % 10) if self.colours else " ")
            rows.append(("".join(text), "".join(classes)))
        self.last = (inputs, rows)
        return rows


# ---- the zoom: an endless dive into the same fractal

DIVE = 0.12                    # e-folds of zoom a second, whatever plays
LEVEL_PUSH = 0.25              # more for every second of full level
BASS_PUSH = 0.1                # and of full bass
LOOP_DEPTH = 1e-5              # the view never gets smaller than this part of the preset's view
LOOP_RANGE = (4.0, 60.0)       # how much zoom one loop may take
NO_LOOP_ZOOM = 30.0            # without a loop point the dive stops this deep


def loop_points(formula, center, half):
    """Points Newton's method keeps coming back to every 2 or 3 steps, pushing everything near
    them away (a repelling cycle): [(z, multiplier, period)]. Around such a point the picture is
    the same picture again `multiplier` times smaller, turned by its angle, which is what lets the
    zoom go on for ever. Searched from a grid of starting points over the view, in a fixed order."""
    f, slope = formula.value, formula.slope
    h = half * 1e-6

    def step(z):
        return z - f(z) / slope(z)

    def gain(z):  # the derivative of a Newton step: f f'' / f'^2, with f'' from the slope
        return f(z) * (slope(z + h) - slope(z - h)) / (2 * h) / slope(z) ** 2

    found = []
    for period in (2, 3):
        for sy in range(-4, 5):
            for sx in range(-8, 9):
                z = center + complex(sx * 0.18, sy * 0.2) * half
                try:
                    for _ in range(60):  # Newton's method on N^period(z) - z
                        orbit = [z]
                        for _ in range(period - 1):
                            orbit.append(step(orbit[-1]))
                        miss = step(orbit[-1]) - z
                        if abs(miss) < 1e-13 * (1 + abs(z)):
                            break
                        multiplier = 1
                        for w in orbit:
                            multiplier *= gain(w)
                        z -= miss / (multiplier - 1)
                    orbit = [z]
                    for _ in range(period):
                        orbit.append(step(orbit[-1]))
                    if abs(orbit[-1] - z) > 1e-10 * (1 + abs(z)):
                        continue
                    if min(abs(orbit[k + 1] - orbit[k]) for k in range(period)) < 1e-6 * half:
                        continue  # a root: it stays where it is
                    multiplier = 1
                    for w in orbit[:-1]:
                        multiplier *= gain(w)
                except (ZeroDivisionError, OverflowError, ValueError):
                    continue
                if abs(multiplier) > 1 and all(abs(z - q[0]) > 1e-6 * half for q in found):
                    found.append((z, multiplier, period))
    return found


def loop_point(formula, center, half):
    """The loop point to dive into: in the middle part of the view, nearest its centre, with a loop
    of 4x to 60x zoom when there is one. None when Newton's method has no such point in view."""
    points = loop_points(formula, center, half)
    for low, high, reach in (LOOP_RANGE + ((1.2, 0.7),), (1.5, 1000.0, (1.8, 1.0))):
        near = [p for p in points if low <= abs(p[1]) <= high
                and abs((p[0] - center).real) < reach[0] * half and abs((p[0] - center).imag) < reach[1] * half]
        if near:
            return min(near, key=lambda p: (round(abs(p[0] - center) / half, 3), round(cmath.phase(p[0] - center), 3)))
    return None


def gone_by(ctx, band, since_ms):
    """How much of a band went by between since_ms and now (ctx.travel at two moments)."""
    now = ctx.travel(band)
    at, ctx.position_ms = ctx.position_ms, since_ms
    before = ctx.travel(band)
    ctx.position_ms = at
    return now - before


class Zoom(Fractal):
    """The fractal's presets, zooming in for as long as the song plays."""
    name = "zoom"
    aliases = ("zm",)
    key = ""
    summary = "full-screen play: an endless zoom into the fractal, pushed by the music (zoom presets)"
    remembered = "zoom_preset"
    help = (("1 .. 9 0", "zoom: dive into one of the first ten presets"),
            ("↑ ↓", "zoom: the previous or next preset"),
            ("+ -", "zoom: nearer and further"), ("c", "zoom: colours on and off"))

    def reset(self):
        super().reset()
        self.loop = None           # (point, multiplier, period) or () when there is none
        self.since = None          # project ms the dive began, taken from the first frame after a choice
        self.depth = 0.0           # e-folds of zoom so far, for the status line

    def loop_for(self, p):
        if self.loop is None:
            center, half = self.frame_for(p)
            self.loop = loop_point(p["formula"], center, half) or ()
        return self.loop

    def status(self, ctx):
        return f"{super().status(ctx)}  ×{math.exp(self.depth):.3g}"

    def frame(self, ctx, width, height):
        p = self.preset()
        t = ctx.position_ms / 1000
        high, level, onset = ctx.band("high", 80), ctx.band("level", 400), ctx.band("onset", 60)
        center0, half0 = self.frame_for(p)
        loop = self.loop_for(p)
        if getattr(ctx, "offline", False):
            since = getattr(ctx, "choice_ms", 0.0)  # every process drawing a video agrees on when the dive began
        else:
            if self.since is None or ctx.position_ms < self.since:  # opened, a new choice, or moved back before the dive
                self.since = ctx.position_ms
            since = self.since
        depth = (DIVE * max(0.0, t - since / 1000) + LEVEL_PUSH * gone_by(ctx, "level", since)
                 + BASS_PUSH * gone_by(ctx, "low", since) - math.log(self.zoom_factor))
        if not loop:
            depth = min(depth, math.log(NO_LOOP_ZOOM))
        self.depth = depth
        depth -= math.log(1.25 - 0.45 * level)  # the level breathes in and out on top, as in the fractal
        theta = 0.06 * t + 0.5 * ctx.travel("low") + 0.15 * ctx.travel("high")
        target, multiplier, period = loop or (center0, 0, 0)
        half = half0 * math.exp(-depth)
        center = target + (center0 - target) * math.exp(-2 * max(0.0, depth))  # the loop point comes to the middle
        extra = 12
        if loop:
            floor = half0 * LOOP_DEPTH
            if half < floor:  # deeper than the loop depth: the same picture a whole number of loops up
                loops = math.ceil(math.log(floor / half) / math.log(abs(multiplier)))
                half *= abs(multiplier) ** loops
                theta += loops * cmath.phase(multiplier)
            extra = 6 + math.ceil(period * math.log(max(1.0, half0 / half)) / math.log(abs(multiplier)))
        limit = LIMIT + extra      # deeper points take more steps to leave the edge
        lift = 1.5 * high + 2.0 * onset
        hue = int(ctx.travel("mid") / 3)
        inputs = (width, height, id(p), self.colours, round(theta, 5), round(math.log(half), 6),
                  round(center.real, 14), round(center.imag, 14), round(lift, 3), hue)
        if self.last is not None and self.last[0] == inputs:
            return self.last[1]
        began = time.monotonic()
        step = self.step
        cols = -(-width // step)
        turn = cmath.exp(1j * theta) * half / (height / 2)
        points = [center + complex((x * step - width / 2) * 0.5, height / 2 - y) * turn
                  for y in range(height) for x in range(cols)]
        steps, finals = p["formula"].newton()(points, limit, 1.0, half0 * 2e-6)  # a fixed tolerance keeps loops alike
        spent = time.monotonic() - began
        budget = 1 / self.fps
        if getattr(ctx, "offline", False):
            self.step = 1
        elif spent > 1.4 * budget and self.step < 3:
            self.step += 1
        elif spent < 0.4 * budget and self.step > 1:
            self.step -= 1
        settled = sorted(s for s in steps if s < limit) or [0]
        fast, slow = settled[int(len(settled) * 0.02)], settled[int(len(settled) * 0.97)]
        scale = (len(RAMP) - 1) / max(1, slow - fast)
        tolerance = half0 * 1e-3
        rows = []
        for y in range(height):
            text, classes = [], []
            base = y * cols
            for x in range(width):
                i = base + x // step
                s = steps[i]
                if s >= limit:
                    text.append(" ")
                    classes.append(" ")
                    continue
                text.append(RAMP[min(len(RAMP) - 1, max(1, int((s - fast) * scale + lift)))])
                classes.append(str((self.root_of(finals[i], tolerance) + hue) % 10) if self.colours else " ")
            rows.append(("".join(text), "".join(classes)))
        self.last = (inputs, rows)
        return rows


def register(gout):
    gout.requires(1)  # the addon API this file is written for (docs/addons.md)
    gout.add_screen(Fractal())
    gout.add_screen(Zoom())
