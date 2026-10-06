"""Sound in plain Python, for instruments: waves, an envelope, and putting notes into a buffer.

Everything works on array("f") at a given sample rate, in loops the interpreter runs at about
two million samples a second: a bar of sixteenth notes takes a tenth of a second.
"""
from __future__ import annotations

import math
from array import array

WAVES = ("sine", "saw", "square", "triangle")
FADE_S = 0.002   # the shortest way in and out of a note, so its edges never click


def silence(count: int) -> array:
    return array("f", bytes(4 * max(0, count)))


def wave(shape: str, hz: float, count: int, rate: int) -> array:
    """`count` samples of a wave at full level, starting at a zero crossing going up. The saw and
    the square have their jumps rounded over one sample each side (polyBLEP), which keeps the
    fold-back of their harmonics past half the sample rate low."""
    out = silence(count)
    inc = hz / rate
    if shape == "sine":
        turn = 2 * math.pi * inc
        sin = math.sin
        for i in range(count):
            out[i] = sin(turn * i)
        return out
    if shape == "triangle":
        phase = 0.25  # the rising zero crossing
        for i in range(count):
            out[i] = 1.0 - 4.0 * abs(phase - 0.5)
            phase += inc
            if phase >= 1.0:
                phase -= 1.0
        return out
    if shape not in ("saw", "square"):
        raise ValueError(f"no wave called {shape!r}: {', '.join(WAVES)}")
    phase = 0.5 if shape == "saw" else 0.0
    square = shape == "square"
    for i in range(count):
        if square:
            value = 1.0 if phase < 0.5 else -1.0
            other = phase + 0.5 if phase < 0.5 else phase - 0.5  # the falling edge is half a turn away
            if other < inc:
                t = other / inc
                value -= t + t - t * t - 1.0
            elif other > 1.0 - inc:
                t = (other - 1.0) / inc
                value -= t * t + t + t + 1.0
            if phase < inc:
                t = phase / inc
                value += t + t - t * t - 1.0
            elif phase > 1.0 - inc:
                t = (phase - 1.0) / inc
                value += t * t + t + t + 1.0
        else:
            value = 2.0 * phase - 1.0
            if phase < inc:
                t = phase / inc
                value -= t + t - t * t - 1.0
            elif phase > 1.0 - inc:
                t = (phase - 1.0) / inc
                value -= t * t + t + t + 1.0
        out[i] = value
        phase += inc
        if phase >= 1.0:
            phase -= 1.0
    return out


def shape_note(samples: array, held: int, decay_s: float, rate: int, level: float = 1.0) -> array:
    """A note's loudness over its life, applied in place: in over 2 ms, dying away so that it is
    a thousandth of its level (-60 dB) after decay_s (0: no dying away), and from sample `held`
    on, where its step ends, down to nothing in a straight line over what is left."""
    count = len(samples)
    rise = max(1, min(round(FADE_S * rate), held))
    fall = math.exp(-math.log(1000.0) / (decay_s * rate)) if decay_s > 0 else 1.0
    tail = max(1, count - held)
    gain = level
    for i in range(count):
        value = samples[i] * gain
        if i < rise:
            value *= i / rise
        if i >= held:
            value *= 1.0 - (i - held) / tail
        samples[i] = value
        gain *= fall
    return samples


def tone(shape: str, hz: float, held: int, release: int, decay_s: float, rate: int, level: float = 1.0) -> array:
    """One note: `held` samples while its step lasts, `release` more while it rings out."""
    release = max(release, round(FADE_S * rate))
    return shape_note(wave(shape, hz, held + release, rate), held, decay_s, rate, level)


def add(buffer: array, at: int, samples: array) -> None:
    """Sum samples into buffer from sample `at` on; the buffer grows when they reach past its end."""
    over = at + len(samples) - len(buffer)
    if over > 0:
        buffer.extend(silence(over))
    for i, value in enumerate(samples, at):
        buffer[i] += value


def tile(one_pass: array, length: int, times: int) -> array:
    """A pass of `length` samples, with whatever rings on after it, `times` times over: each tail
    sounds over the start of what follows."""
    head, tail = one_pass[:length], one_pass[length:]
    if len(head) < length:
        head.extend(silence(length - len(head)))
    out = head * times
    out.extend(silence(len(tail)))
    for k in range(1, times + 1):
        add(out, k * length, tail)
    return out
