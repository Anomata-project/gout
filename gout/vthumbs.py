"""Thumbnails of a video file for the window: one JPEG strip with a small picture every few seconds
of the file, cached under .gout/video/. The page cuts the strip up and lays the pictures along a
video track's lane through its time map, so a slowed stretch shows the same picture for longer."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .core import die
from .motion import CACHE_DIR

HEIGHT = 56          # px of a thumbnail
MAX_WIDTH = 120      # and no wider, whatever the picture's shape
MAX_COUNT = 200      # pictures in a strip: a five-minute file gets one every 1.5 s


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def plan(length_ms: int, width: int, height: int) -> dict:
    """How a file's strip is cut: `count` pictures `step_ms` apart (picture k is the file at k * step_ms),
    each `w` by `h` px, side by side."""
    count = max(1, min(MAX_COUNT, -(-length_ms // 1000)))
    return {"count": count, "step_ms": length_ms / count, "w": min(MAX_WIDTH, even(HEIGHT * width / max(1, height))),
            "h": HEIGHT}


def strip(root: Path, path: Path, p: dict) -> Path:
    """The strip for a file, made on first use."""
    st = path.stat()
    key = hashlib.sha1(f"1|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{p['count']}|{p['step_ms']:.3f}|{p['w']}".encode()).hexdigest()[:20]
    cache = root / CACHE_DIR
    out = cache / f"{key}.thumbs.jpg"
    if out.exists():
        return out
    cache.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(f".{os.getpid()}.part.jpg")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-an", "-vf",
         f"fps={1000 / p['step_ms']:.9f},scale={p['w']}:{p['h']}:flags=area,tile={p['count']}x1",
         "-frames:v", "1", "-q:v", "4", str(part)], capture_output=True)
    if result.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        die("ffmpeg could not make thumbnails of " + path.name)
    part.replace(out)
    return out
