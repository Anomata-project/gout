"""The notes a reconstruction gives, and the intervals between them: in Hz, in cents, as ratios.

Nothing here knows a scale. A note is the lowest resonance of a fingering; an interval is the
distance between two notes in cents (1200 to the octave), with the simple ratio nearest to it
and how far off that is.
"""
from __future__ import annotations

import math
from itertools import product

from .geometry import Setup

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def cents(hz: float, ref: float) -> float:
    return 1200 * math.log2(hz / ref)


def pitch_name(hz: float, a4: float = 440.0) -> str:
    """The nearest note of twelve to the octave and how far off it is, in cents: only a way to
    say where a frequency lies, not a claim that anyone meant that note."""
    semis = 69 + 12 * math.log2(hz / a4)
    nearest = round(semis)
    return f"{NAMES[nearest % 12]}{nearest // 12 - 1}{round((semis - nearest) * 100):+d}"


def nearest_ratio(interval_cents: float, limit: int = 8) -> tuple[int, int, float]:
    """(p, q, cents off) for the ratio p/q of whole numbers up to limit (p up to twice that, for
    intervals past the octave) that lies nearest, the simplest first among those equally near.
    With numbers this small a ratio is never far away: the cents off say how much it means."""
    best = None
    for q in range(1, limit + 1):
        p = max(1, round(q * 2 ** (interval_cents / 1200)))
        for cand in (p - 1, p, p + 1):
            if cand < 1 or cand > 2 * limit or math.gcd(cand, q) != 1:
                continue
            off = interval_cents - 1200 * math.log2(cand / q)
            key = (round(abs(off), 1), cand + q)
            if best is None or key < best[0]:
                best = (key, cand, q, off)
    return best[1], best[2], best[3]


def all_fingerings(names: list[str]) -> list[dict[str, bool]]:
    """Every way of opening and closing the holes: all closed first, then by how many are open."""
    found = [dict(zip(names, states)) for states in product((False, True), repeat=len(names))]
    return sorted(found, key=lambda f: (sum(f.values()), [not f[n] for n in reversed(names)]))


def ladder(names: list[str]) -> list[dict[str, bool]]:
    """The plain fingerings: all closed, then the holes opened one after another from the far end
    towards the mouth, as the fingers come off a pipe."""
    return [{name: i >= len(names) - k for i, name in enumerate(names)} for k in range(len(names) + 1)]


def label(names: list[str], fingering: dict[str, bool]) -> str:
    """A fingering from the mouth end to the far end: x closed, o open."""
    return "".join("o" if fingering[name] else "x" for name in names)


class Note:
    def __init__(self, fingering: dict[str, bool], text: str, hz: float | None, q: float | None, above: list[float]):
        self.fingering, self.text, self.hz, self.q, self.above = fingering, text, hz, q, above


def notes(setup: Setup, fingerings: list[dict[str, bool]] | None = None) -> list[Note]:
    """The lowest resonance of each fingering (None when there is none in reach), with the
    resonances above it, which blowing harder would reach."""
    out = []
    for fingering in fingerings if fingerings is not None else all_fingerings(setup.names):
        found = setup.resonances(fingering)
        out.append(Note(fingering, label(setup.names, fingering), found[0].hz if found else None,
                        found[0].q if found else None, [r.hz for r in found[1:]]))
    return out


def steps(found: list[Note]) -> list[float | None]:
    """The interval from each note to the next, in cents."""
    return [cents(b.hz, a.hz) if a.hz and b.hz else None for a, b in zip(found, found[1:])]
