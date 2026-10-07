"""Instrument tracks in a project: making one, writing its wav from its pattern, keeping the wavs in step.

An instrument track is an ordinary track whose file gout writes itself. Whatever decides how it
sounds (the instrument, the pattern, the first row's cells, the tempo, the sample rate) is hashed,
and the hash of what a wav was written from is kept beside it. settle() writes again every wav
whose hash no longer matches: after an edit, an undo, an import, a new tempo.
"""
from __future__ import annotations

import hashlib
import json
import math
from array import array

from .core import die, GoutError, TRACK_DIR
from .inst import InstContext, Instrument, instrument, MAX_TAIL_S
from .media import write_float_wav
from .pattern import Pattern, step_seconds
from .settings import project_bpm
from .synthkit import tile

DEFAULT_BPM = 120.0
FORMAT = 1  # of the wavs this module writes: raise it and every instrument track is written again


def tempo(project) -> float:
    return project_bpm(project) or DEFAULT_BPM


def context(project, item: Instrument, settings: dict) -> InstContext:
    bpm = tempo(project)
    return InstContext(project.rate, bpm, step_seconds(bpm, item.value(settings, "step")), project.root)


def state_of(project, data: dict, item: Instrument) -> str:
    """A hash of everything that decides what an instrument track sounds like."""
    what = [FORMAT, item.name, item.version, data["settings"], data["pattern"], tempo(project), project.rate]
    return hashlib.sha1(json.dumps(what, sort_keys=True).encode()).hexdigest()


def play(project, t: dict, item: Instrument) -> tuple[list[array], int]:
    """The whole of an instrument track as samples per channel: the pattern as many times as it
    loops, the last pass ringing out. And the length of one pass in samples."""
    data = t["instrument"]
    settings = data["settings"]
    try:
        pattern = Pattern.from_json(data["pattern"], item.features)
        ctx = context(project, item, settings)
        try:
            item.check(ctx, pattern, settings)
        except ValueError as exc:   # the instrument's own no: a cell out of its range, a pattern it cannot play
            die(f"{t['name']}: {exc}")
        chans = [c if isinstance(c, array) and c.typecode == "f" else array("f", c)
                 for c in item.render(ctx, pattern, settings)]
    except GoutError:
        raise
    except Exception as exc:  # an addon's instrument must fail with its name on it
        die(f"{t['name']}: the {item.name} could not play its pattern ({type(exc).__name__}: {exc})")
    if len(chans) not in (1, 2) or len({len(c) for c in chans}) != 1:
        die(f"{t['name']}: the {item.name} gave {len(chans)} channels of unequal length; one or two alike are wanted")
    if not all(math.isfinite(sum(c)) for c in chans):
        die(f"{t['name']}: the {item.name} gave samples that are not numbers")
    length = ctx.edges(pattern)[-1]
    most = length + round(MAX_TAIL_S * ctx.rate)
    return [tile(c[:most], length, item.value(settings, "loop")) for c in chans], length


def write_track(project, t: dict, item: Instrument, chans: list[array] | None = None) -> int:
    """Write an instrument track's wav from its pattern (or from the samples play() already gave
    for it), and what the project knows of the file. Returns its length in ms."""
    state = state_of(project, t["instrument"], item)
    if chans is None:
        chans, _ = play(project, t, item)
    path = project.tracks_dir / t["file"]
    write_float_wav(path, chans, project.rate)
    length_ms = round(len(chans[0]) * 1000 / project.rate)
    project.update(t["n"], length_ms=length_ms, channels=len(chans), sample_rate=project.rate)
    project.forget_envelope(t["file"])
    project.envelope(t["file"], path)  # so the timeline draws it without a pause later
    project.rendered_set(t["file"], state)
    return length_ms


def settle(project) -> list[str]:
    """Write again every instrument track whose wav is not what its pattern says, or is missing.
    Returns the names of the tracks written. A track whose instrument is not installed keeps the
    wav it has; one that cannot be played says so and keeps it too."""
    played = project.all_instruments()
    if not played:
        return []
    known = project.rendered()
    done = []
    for t in project.tracks():
        data = t.get("instrument")
        item = instrument(data["kind"]) if data else None
        if item is None:
            continue
        if known.get(t["file"]) == state_of(project, data, item) and (project.tracks_dir / t["file"]).exists():
            continue
        try:
            write_track(project, t, item)
            done.append(t["name"])
        except GoutError as exc:
            print(f"      {exc}; {TRACK_DIR}/{t['file']} is left as it was")
    return done


def new_track(project, item: Instrument, name: str | None, at_ms: int) -> dict:
    """A new instrument track with an empty pattern, its (silent) wav written."""
    track_name = project.unique_name(name or item.name)
    file = f"{track_name}.wav"
    t = project.insert(name=track_name, file=file, kind="wav", length_ms=0, channels=1, sample_rate=project.rate,
                       offset_ms=at_ms)
    project.instrument_set(file, item.name, {}, Pattern().to_json())
    t = project.track(str(t["n"]))
    write_track(project, t, item)
    return project.track(str(t["n"]))


def users(project) -> list[dict]:
    """The instrument tracks, each with its instrument (None when it is not installed) under "item"."""
    out = []
    for t in project.tracks():
        if t.get("instrument"):
            out.append({**t, "item": instrument(t["instrument"]["kind"])})
    return out
