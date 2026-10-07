"""A pipe's sound, made from what the acoustics computes for it.

No recording is used, and the jet at the lips is not simulated. A note is the resonance its
fingering gives. Its overtones are as strong as the pipe lets them through: the impedance the jet
meets at twice, three times ... the note's frequency, set against the impedance at the note
itself, on top of a drive that falls off towards the high overtones (less so the harder it is
blown). The breath is noise through the same resonances. So a fingering whose overtones fall
between the pipe's resonances sounds plain, as a bottle does, and one whose overtones meet them
sounds bright. What a player's lips and breath add to that is not in it.

Plain Python, about a second of work for two seconds of sound.
"""
from __future__ import annotations

import math
import random
import wave
from array import array

from .geometry import Setup

TABLE = 1024          # samples in one period of a note's wave
ATTACK_S, RELEASE_S, GLIDE_S = 0.045, 0.060, 0.020


class Timbre:
    """One note: its frequency, the weight of each overtone, and the resonances the breath's noise rings in."""

    def __init__(self, hz: float, weights: list[float], rings: list[tuple[float, float]]):
        self.hz, self.weights, self.rings = hz, weights, rings
        self.table = one_period(weights)


def one_period(weights: list[float], size: int = TABLE) -> array:
    """A period of the wave these overtone weights make, its largest swing 1."""
    out = [0.0] * size
    for n, weight in enumerate(weights, 1):
        if weight < 1e-4:
            continue
        turn = 2 * math.pi * n / size
        for i in range(size):
            out[i] += weight * math.sin(turn * i)
    top = max(abs(v) for v in out) or 1.0
    return array("f", (v / top for v in out))


def timbre(setup: Setup, fingering: dict[str, bool], register: int = 0, hard: float = 0.3, rate: int = 48000,
           most: int = 32) -> Timbre | None:
    """The note a fingering gives: its lowest resonance (register 0) or one above, reached by
    blowing harder. hard, 0 to 1, is how hard it is blown: the overtones' drive falls off as
    1/n^2 at 0 and as 1/n at 1. None when the pipe has no such resonance."""
    found = setup.resonances(fingering)
    if register >= len(found):
        return None
    hz = found[register].hz
    opened = setup.opened(fingering)
    met = abs(setup.pipe.met_by_the_jet(hz, opened, setup.far, setup.mouth))
    slope = 2.0 - max(0.0, min(1.0, hard))
    weights = [1.0]
    for n in range(2, most + 1):
        if n * hz > min(16000.0, 0.45 * rate):
            break
        weights.append(met / abs(setup.pipe.met_by_the_jet(n * hz, opened, setup.far, setup.mouth)) / n ** slope)
    return Timbre(hz, weights, [(r.hz, max(4.0, min(r.q, 60.0))) for r in found[:3]])


def render(parts: list[tuple[float, Timbre]], rate: int = 48000, level: float = 0.5, breath: float = 0.12,
           seed: int = 1) -> array:
    """One breath: parts are (seconds, timbre) played one after another without a new attack, the
    wave gliding from each to the next. Returns the samples, with the release after the last part."""
    counts = [max(1, round(seconds * rate)) for seconds, _ in parts]
    held = sum(counts)
    release = round(RELEASE_S * rate)
    total = held + release
    out = array("f", bytes(4 * total))
    attack, glide = max(1, round(ATTACK_S * rate)), max(1, round(GLIDE_S * rate))
    rng = random.Random(seed)
    phase, start = 0.0, 0
    for k, ((_, voice), count) in enumerate(zip(parts, counts)):
        last = k == len(parts) - 1
        end = start + count + (release if last else 0)
        table, step = voice.table, voice.hz * TABLE / rate
        before = parts[k - 1][1] if k else None
        # the breath: noise through two-pole resonators at the pipe's resonances, as loud as `breath` of the note
        hiss = array("f", bytes(4 * (end - start)))
        for hz, q in voice.rings:
            if hz >= 0.45 * rate:
                continue
            r = math.exp(-math.pi * hz / (q * rate))
            a1, a2, y1, y2 = 2 * r * math.cos(2 * math.pi * hz / rate), -r * r, 0.0, 0.0
            for j in range(end - start):
                y = rng.uniform(-1.0, 1.0) + a1 * y1 + a2 * y2
                y2, y1 = y1, y
                hiss[j] += y
        loud = math.sqrt(sum(v * v for v in hiss) / len(hiss)) or 1.0
        air = breath * 0.6 / loud
        for i in range(start, end):
            at = int(phase)
            frac = phase - at
            value = table[at] + (table[(at + 1) % TABLE] - table[at]) * frac
            if before is not None and i - start < glide:   # the wave of the fingering before fades into this one
                old = before.table[at] + (before.table[(at + 1) % TABLE] - before.table[at]) * frac
                value = old + (value - old) * (i - start) / glide
            gain = level
            if i < attack:
                gain *= 0.5 - 0.5 * math.cos(math.pi * i / attack)
            if i >= held:
                gain *= 0.5 + 0.5 * math.cos(math.pi * (i - held) / release)
            out[i] = gain * (value + air * hiss[i - start])
            phase += step
            if phase >= TABLE:
                phase -= TABLE
        start += count
    return out


def write_wav(path, samples: array, rate: int = 48000) -> None:
    """A 16-bit mono wav, with the standard library."""
    top = max((abs(v) for v in samples), default=0.0)
    scale = 32767 * min(1.0, 0.98 / top) if top > 0.98 else 32767
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(array("h", (int(v * scale) for v in samples)).tobytes())
