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


# ---- parts: a track cut into pieces that stay on its line

CROSS_MS = 5          # where two parts meet, each reaches this far over the cut and they crossfade
MIN_PART_MS = 20      # no part shorter than this
PART_WORD = "p"       # unnamed parts are p1, p2 ... from left to right


def part_owner(track_file: str, pid: int) -> str:
    """The effect chain owner of a part (track files never have a # in their name)."""
    return f"{track_file}#{pid}"


def part_start(t: dict, part: dict) -> int:
    """Where a part begins on the timeline."""
    return t["offset_ms"] + part["shift_ms"] + part["in_ms"]


def part_label(parts: list[dict], part: dict) -> str:
    """The name a part answers to: its own, or p and its place."""
    return part["name"] or f"{PART_WORD}{parts.index(part) + 1}"


def part_has_settings(part: dict) -> bool:
    return bool(part["gain_db"] or abs(part["pan"]) >= 0.005 or part["mute"] or part.get("fx"))


def part_settings(part: dict) -> tuple:
    """What a part sounds like apart from where it is: parts alike here can be joined as they are."""
    chain = tuple((i["kind"], i["params"], i["on"]) for i in part.get("fx") or [])
    return round(part["gain_db"], 3), round(part["pan"], 3), bool(part["mute"]), chain


def meets(left: dict, right: dict) -> bool:
    """Whether right carries on exactly where left stops: a cut to crossfade over."""
    return left["out_ms"] == right["in_ms"] and left["shift_ms"] == right["shift_ms"]
