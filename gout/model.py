"""Where a track sits on the timeline and whether the mix hears it."""
from __future__ import annotations


def audible(t: dict) -> tuple[int, int]:
    """(in, out) of the track in file time, out defaulting to the file end."""
    out = t["out_ms"] if t["out_ms"] is not None else t["length_ms"]
    return t["in_ms"], min(out, t["length_ms"])


def timeline(t: dict) -> tuple[int, int]:
    """(start, end) of the audible part on the project timeline."""
    a, b = audible(t)
    return t["offset_ms"] + a, t["offset_ms"] + b


def is_heard(t: dict, any_solo: bool) -> bool:
    if t["mute"]:
        return False
    if any_solo and not t["solo"]:
        return False
    a, b = audible(t)
    return b > a
