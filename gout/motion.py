"""How much changes in a picture, over time: the curve a video track is synced by.

ffmpeg shrinks every frame to 64 by 36 and `signalstats` reports how far each frame is from the one
before (mean absolute difference of luma and both chroma planes). That becomes one number per 25 ms of
the file, the same grid as the music's features in analysis.py, as the rate of change: the difference
divided by the time between the two frames, so a file at 12 fps and one at 60 read alike.

The curve is cached per file under .gout/video/, keyed by size and modification time like the music
analysis, so a picture is read once.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from array import array
from pathlib import Path

from .analysis import FRAME_MS
from .core import die

VERSION = 1
CACHE_DIR = ".gout/video"
SIZE = "64:36"
CLIP = 2.0  # a frame's change is cut at this many times the 95th percentile: a scene cut is an event, not motion

FRAME = re.compile(r"frame:\d+\s+pts:-?\d+\s+pts_time:(-?[\d.]+)")
DIF = re.compile(r"lavfi\.signalstats\.([YUV])DIF=([\d.]+)")


def read_changes(path: Path) -> list[tuple[float, float]]:
    """(time in seconds, change since the frame before) for every frame of the file."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf",
         f"scale={SIZE}:flags=area,signalstats,metadata=mode=print:file=-", "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        die(f"ffmpeg could not read the picture of {path.name}: " + (result.stderr.strip().splitlines() or ["no reason given"])[-1])
    frames: list[tuple[float, float]] = []
    time, change = None, 0.0
    for line in result.stdout.splitlines():
        found = FRAME.match(line)
        if found:
            if time is not None:
                frames.append((time, change))
            time, change = float(found.group(1)), 0.0
            continue
        found = DIF.match(line)
        if found:
            change += float(found.group(2))
    if time is not None:
        frames.append((time, change))
    return frames


def on_grid(frames: list[tuple[float, float]], length_ms: int) -> array:
    """The rate of change per 25 ms cell of the file: a frame's change spread over the time since
    the frame before it, the ends filled in from what is next to them."""
    cells = max(1, -(-length_ms // FRAME_MS))
    if len(frames) < 2:
        return array("f", [0.0] * cells)
    changes = sorted(c for _, c in frames[1:])
    cap = CLIP * changes[min(len(changes) - 1, int(len(changes) * 0.95))]
    out = array("f", [0.0] * cells)
    for (t0, _), (t1, change) in zip(frames, frames[1:]):
        if t1 <= t0:
            continue
        rate = (min(change, cap) if cap > 0 else change) / (t1 - t0)
        for c in range(max(0, int(t0 * 1000 // FRAME_MS)), min(cells, int(-(-t1 * 1000 // FRAME_MS)))):
            out[c] = rate
    first = frames[1][0] * 1000 // FRAME_MS
    for c in range(int(min(first, cells))):  # before the second frame there is no rate: take the first one's
        out[c] = out[int(min(first, cells - 1))]
    return out


def curve(path: Path, cache_dir: Path, length_ms: int) -> array:
    """The motion curve of a video file, from the cache when it is there."""
    st = path.stat()
    key = hashlib.sha1(f"v{VERSION}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{length_ms}".encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}.motion"
    if cached.exists():
        data = array("f")
        data.frombytes(cached.read_bytes())
        return data
    data = on_grid(read_changes(path), length_ms)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(f".{os.getpid()}.part")
    tmp.write_bytes(data.tobytes())
    tmp.replace(cached)
    return data
