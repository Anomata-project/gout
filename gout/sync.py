"""Fitting a video track to the music: read the picture's motion and the music's level, make the
time map (timemap.py), store it as warp points on the track."""
from __future__ import annotations

from .analysis import FRAME_MS, project_features
from .core import die
from .model import audible, is_heard, timeline
from .motion import CACHE_DIR, curve
from .settings import project_bpm
from .timemap import fit, simplify, slowdowns


def music_range(project) -> tuple[int, int]:
    """From where the music is first heard to where it ends: what a picture is fitted to by default."""
    tracks = project.tracks()
    any_solo = any(t["solo"] for t in tracks)
    spans = [timeline(t) for t in tracks if is_heard(t, any_solo)]
    if not spans:
        die("there is nothing to fit a picture to: add a sound first (gout add FILE)")
    return max(0, min(a for a, _ in spans)), max(b for _, b in spans)


def fit_track(project, t: dict, start_ms: int, end_ms: int, *, slow: float, fast: float, depth: float,
              tail: str) -> dict:
    """Make the map of video track t over start_ms .. end_ms of the project and store it. Returns what
    to tell the user: the fit and the warp points."""
    start_ms = start_ms // FRAME_MS * FRAME_MS
    end_ms = end_ms // FRAME_MS * FRAME_MS
    if end_ms - start_ms < 1000:
        die(f"a stretch of {(end_ms - start_ms) / 1000:g} s is too short to fit a picture to")
    a, b = audible(t)
    path = project.tracks_dir / t["file"]
    motion = list(curve(path, project.root / CACHE_DIR, t["length_ms"]))[a // FRAME_MS:b // FRAME_MS]
    if len(motion) < 2:
        die(f"track {t['n']} {t['name']} shows less than 50 ms: nothing to fit")
    features = project_features(project)
    first = start_ms // FRAME_MS
    cells = (end_ms - start_ms) // FRAME_MS
    level = [features.values["level"][i] if i < features.frames else 0.0 for i in range(first, first + cells)]
    onset = [features.values["onset"][i] if i < features.frames else 0.0 for i in range(first, first + cells)]
    bars: list[int] = []
    bpm = project_bpm(project)
    if bpm:
        bar_ms = 240000 / bpm  # four beats
        k = -(-start_ms // bar_ms)
        while k * bar_ms < end_ms:
            bars.append(round((k * bar_ms - start_ms) / FRAME_MS))
            k += 1
    result = fit(motion, level, onset, (b - a) / FRAME_MS, slow=slow, fast=fast, depth=depth, tail=tail, bars=bars)
    points = simplify(result.src, a, set(bars))
    project.update(t["n"], offset_ms=start_ms)
    project.video_set(t["file"], warp=points, want_ms=[0, cells * FRAME_MS], slow=slow, fast=fast, depth=depth, tail=tail)
    return {"fit": result, "points": points, "start": start_ms, "end": end_ms, "bars": len(bars)}


def stretch_text(points: list[list[int]]) -> str:
    """How much slower than the file the map shows it: on average, and from the quickest to the slowest stretch."""
    if len(points) < 2 or points[-1][1] <= points[0][1]:
        return "no stretch"
    ratios = [r for r in slowdowns(points) if r != float("inf")]
    average = (points[-1][0] - points[0][0]) / (points[-1][1] - points[0][1])
    return f"{average:.2f}x on average, {min(ratios):.2f}x to {max(ratios):.2f}x"
