"""The plain synth: one wave, one note at a time, a level for each step."""
from __future__ import annotations

from array import array

from ..inst import Instrument
from ..pattern import DECAY, Feature, NOTE, note_hz, RELEASE
from ..synthkit import add, silence, tone, WAVES

HEADROOM = 0.5  # a note at level 100 peaks at -6 dBFS, which leaves room for the tail of the one before


class Synth(Instrument):
    name = "synth"
    aliases = ("syn",)
    summary = "a plain synth: a sine, saw, square or triangle wave, one note at a time"
    features = (NOTE, DECAY, RELEASE,
                Feature("level", "number", 100.0, 0, 100, "%", summary="how loud the step is: an accent, or a ghost note"))
    settings = (Feature("wave", "choice", "saw", choices=WAVES, summary="the shape of the wave"),)

    def render(self, ctx, pattern, settings) -> list[array]:
        note, decay, release, level = self.features
        shape = self.value(settings, "wave")
        edges = ctx.edges(pattern)
        out = silence(edges[-1])
        for step, midi, steps in pattern.notes(note):
            start = edges[step - 1]
            held = edges[min(step - 1 + steps, pattern.steps)] - start
            ring = round(pattern.value(release, step) / 1000 * ctx.rate)
            add(out, start, tone(shape, note_hz(midi), held, ring, pattern.value(decay, step) / 1000, ctx.rate,
                                 HEADROOM * pattern.value(level, step) / 100))
        return [out]
