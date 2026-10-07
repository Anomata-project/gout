"""The bone pipe: an instrument whose notes are not typed but follow from a pipe's geometry.

A thin bridge to the bonepipe package (bonepipe/ in this repository), which holds the objects,
what was measured on them and the acoustics. Here a step says how it is blown and which holes
are open; bonepipe says what that sounds like. The cells of the first row are the reconstruction:
which object, which end is blown, how much the lips leave open, how long the tube was, and so on.
Each of them is something nobody knows, so each can be changed and the pattern heard again.
"""
from __future__ import annotations

from array import array

from ..inst import Instrument
from ..pattern import Feature

HOLD, REST = "=", "rest"
HOLES = ("hole3", "hole1", "hole2", "hole5")     # as they lie along the bone, from the proximal end
SWITCHES = {"half3": "hole3", "half5": "hole5"}  # first-row cells that say whether a half hole was a hole


def library():
    """bonepipe's modules, imported when first wanted."""
    from bonepipe import geometry, objects, scale, sound
    return geometry, objects, scale, sound


def make_settings() -> tuple[Feature, ...]:
    geometry, objects, _, _ = library()
    cells = [Feature("object", "choice", objects.object_ids()[0], choices=tuple(objects.object_ids()), summary="which object is played"),
             Feature("blown", "choice", "proximal", choices=geometry.CHOICES["blown"], summary="the end that is blown"),
             Feature("far", "choice", "open", choices=geometry.CHOICES["far"], summary="the other end: open, or closed by a hand"),
             Feature("half3", "toggle", True, summary="whether the half hole at the proximal end was a hole"),
             Feature("half5", "toggle", False, summary="whether the half hole on the anterior side was a hole")]
    thing = objects.load(objects.object_ids()[0])   # the ranges that follow from measurements are this object's
    start, limits = geometry.defaults(thing), geometry.limits(thing)
    for key, (_, _, _, unit, what) in geometry.NUMBERS.items():
        cells.append(Feature(key, "number", start[key], *limits[key], unit, summary=what.split(":")[0]))
    return tuple(cells)


class BonePipe(Instrument):
    name = "bonepipe"
    aliases = ("pipe",)
    summary = "a bone pipe reconstructed from its measurements: fingerings in, the notes its geometry gives out"
    version = 1
    shown = ("object", "blown", "far")   # the other cells of the reconstruction appear in the first row once they are set
    features = (
        Feature("blow", "choice", None, choices=("soft", "hard", HOLD, REST),
                summary="soft: the fingering's lowest note; hard: the one above it; = the breath goes on; nothing: silence"),
        *(Feature(name, "choice", "x", choices=("x", "o"), summary="x closed, o open") for name in HOLES),
        Feature("level", "number", 80.0, 0, 100, "%", summary="how loud the step is"),
    )

    def __init__(self):
        self.settings = make_settings()

    # ---- from the cells of the first row to a pipe that can sound

    def setup(self, settings: dict):
        geometry, objects, _, _ = library()
        thing = objects.load(self.value(settings, "object"))
        given = {"blown": self.value(settings, "blown"), "far": self.value(settings, "far"),
                 "hole3": self.value(settings, "half3"), "hole5": self.value(settings, "half5")}
        for key in geometry.NUMBERS:
            if settings.get(key) is not None:
                given[key] = settings[key]
        return geometry.build(geometry.Reconstruction(thing, **given))   # ValueError says which cell is out of range

    def check(self, ctx, pattern, settings) -> None:
        self.setup(settings)

    def breaths(self, pattern, setup) -> list[list[tuple[int, dict, int]]]:
        """The pattern as breaths: each a run of (step, fingering, register) blown without a break."""
        blow = self.features[0]
        holes = {f.name: f for f in self.features if f.name in HOLES}
        out: list[list[tuple[int, dict, int]]] = []
        register, sounding = 0, False
        for step in range(1, pattern.steps + 1):
            how = pattern.value(blow, step)
            if how in (None, REST) or (how == HOLD and not sounding):
                sounding = False
                continue
            fingering = {name: pattern.value(holes[name], step) == "o" for name in setup.names}
            if how != HOLD:
                register = 1 if how == "hard" else 0
                out.append([])
            sounding = True
            out[-1].append((step, fingering, register))
        return out

    def voices(self, pattern, setup) -> dict:
        """The note of every fingering the pattern uses: {(fingering, register): timbre or None}."""
        _, _, _, sound = library()
        found = {}
        for breath in self.breaths(pattern, setup):
            for _, fingering, register in breath:
                key = (tuple(sorted(fingering.items())), register)
                if key not in found:
                    found[key] = sound.timbre(setup, fingering, register, hard=0.7 if register else 0.3)
        return found

    def render(self, ctx, pattern, settings) -> list[array]:
        _, _, _, sound = library()
        setup = self.setup(settings)
        voices = self.voices(pattern, setup)
        level = self.features[-1]
        edges = ctx.edges(pattern)
        out = array("f", bytes(4 * edges[-1]))
        for breath in self.breaths(pattern, setup):
            parts, first = [], None
            for step, fingering, register in breath:
                voice = voices[(tuple(sorted(fingering.items())), register)]
                if voice is None:       # the pipe has no such note: the breath ends here
                    break
                first = step if first is None else first
                parts.append(((edges[step] - edges[step - 1]) / ctx.rate, voice))
            if not parts:
                continue
            samples = sound.render(parts, ctx.rate, 0.5 * pattern.value(level, first) / 100, seed=first)
            start = edges[first - 1]
            over = start + len(samples) - len(out)
            if over > 0:
                out.extend(array("f", bytes(4 * over)))
            for i, value in enumerate(samples, start):
                out[i] += value
        return [out]

    def reads(self, ctx, pattern, settings) -> list[tuple[str, list[str]]]:
        """What the grid shows under the fingerings: each sounding step's frequency, and the
        interval from the note before it, in cents."""
        _, _, scale, _ = library()
        try:
            setup = self.setup(settings)
        except ValueError:
            return []
        voices = self.voices(pattern, setup)
        hz = [""] * pattern.steps
        cents = [""] * pattern.steps
        before = None
        for breath in self.breaths(pattern, setup):
            for step, fingering, register in breath:
                voice = voices[(tuple(sorted(fingering.items())), register)]
                if voice is None:
                    hz[step - 1] = "none"
                    break
                hz[step - 1] = f"{voice.hz:.0f}"
                if before is not None and abs(voice.hz - before) > 1e-6:
                    cents[step - 1] = f"{scale.cents(voice.hz, before):+.0f}"
                before = voice.hz
        return [("hz", hz), ("cents", cents)]
