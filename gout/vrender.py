"""Rendering video tracks: the pictures of a project, each through its time map, laid over each other,
with master.wav under them. H.264 and AAC in an mp4, like gout's other videos.

One ffmpeg does all of it. For every track a chain of filters:

  setpts      the time map. The track's warp points, inverted (project time from picture time), as one
              expression; with the picture's frames stamped with the project time they belong to,
              everything after this point is ordinary video.
  fps         nearest: each output frame is the picture's frame nearest in time (fast, steppy when slowed),
  framerate   blend: frames mixed by how close they are in time,
  minterpolate flow: motion-compensated interpolation (slow; run at a smaller working size),
  fade, lut   the clip's fades and opacity, in the alpha channel,
  overlay     onto what is below, bottom track first, over black.

and on top of all of them the title, when set title / set artist are there (and no -T): the same big
letters in gout's characters that the screens and covers get (video.Title), drawn once as a picture
of the title's box, wiped in from the left by a geq expression and shown from 0.5 s to 6.5 s.

A track that loops or bounces (sync --loop, --pingpong) is read from a cached cycle file under
.gout/video/ (the trimmed picture, and for a bounce the picture and then the picture backwards) that
ffmpeg repeats; nothing else is ever re-encoded, and the picture file itself is never touched.
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import zlib
from pathlib import Path

from .core import die
from .media import graph_from_file
from .model import audible, video_points
from .motion import CACHE_DIR
from .settings import setting
from .video import Progress

MAX_W, MAX_H = 1920, 1080
PREVIEW_W = 640
FLOW_W = 960            # flow works at this width at most (measured: 0.68 s a frame at 1080p, 0.18 s at 960)
MARGIN = {"nearest": 0.25, "blend": 0.25, "flow": 1.0}  # seconds of picture read past the map's end: interpolation needs a next frame
CHUNK_BYTES = 400e6     # reversing holds this much of the picture at once
OUT = "libx264"


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def fit(width: int, height: int, box_w: int, box_h: int) -> tuple[int, int]:
    """The picture's size scaled down (never up) to fit the box, keeping its shape."""
    scale = min(box_w / width, box_h / height, 1.0)
    return even(width * scale), even(height * scale)


def output_size(videos: list[dict], text: str | None, preview: bool) -> tuple[int, int]:
    """What the video is: -s WxH, else the pictures' own size when they all share one, else 1920x1080;
    never more than 1920x1080, and a preview no wider than 640."""
    if text:
        try:
            w, h = (int(v) for v in text.lower().split("x"))
        except ValueError:
            die(f"-s {text}: use WIDTHxHEIGHT, like 1280x720")
        if w < 16 or h < 16:
            die(f"-s {text}: too small")
        return even(w), even(h)
    sizes = {(t["video"]["width"], t["video"]["height"]) for t in videos}
    w, h = sizes.pop() if len(sizes) == 1 else (MAX_W, MAX_H)
    w, h = fit(w, h, MAX_W, MAX_H)
    return fit(w, h, PREVIEW_W, MAX_H) if preview else (w, h)


# ---- the time map as an expression

def inverse_expression(points: list[tuple[float, float]]) -> str:
    """Project time (seconds) from picture time T, straight between points, as an ffmpeg expression, a
    binary tree of ifs so a long map stays shallow. points are (project s, picture s)."""
    def line(i: int) -> str:
        (p0, s0), (p1, s1) = points[i], points[i + 1]
        return f"{p0:.6f}+(T-{s0:.6f})*{(p1 - p0) / (s1 - s0):.9f}"

    def build(lo: int, hi: int) -> str:
        if hi - lo == 1:
            return line(lo)
        mid = (lo + hi) // 2
        return f"if(lt(T,{points[mid][1]:.6f}),{build(lo, mid)},{build(mid, hi)})"

    return build(0, len(points) - 1)


# ---- a cycle file for loops and bounces

def cycle_file(project, t: dict, tail: str) -> tuple[Path, float]:
    """The trimmed picture as a file ffmpeg can repeat: the picture, and for a bounce the picture and
    then the picture backwards. Cached. Returns (path, seconds one round lasts)."""
    a, b = audible(t)
    src = project.tracks_dir / t["file"]
    st = src.stat()
    key = hashlib.sha1(f"1|{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{a}|{b}|{tail}".encode()).hexdigest()[:20]
    cache = project.root / CACHE_DIR
    path = cache / f"{key}.{tail}.mp4"
    length = (b - a) / 1000
    if path.exists():
        return path, length * (2 if tail == "pingpong" else 1)
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cycle-", dir=cache))
    v = t["video"]
    encode = ["-an", "-c:v", OUT, "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p"]

    def run(*args: str) -> None:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True)
        if result.returncode != 0:
            die("ffmpeg could not prepare the loop: " + (result.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1])

    try:
        print(f"video preparing the {tail} of {t['name']} (once; cached)", flush=True)
        forward = work / "forward.mp4"
        run("-ss", f"{a / 1000:.6f}", "-t", f"{length:.6f}", "-i", str(src), *encode, str(forward))
        files = [forward]
        if tail == "pingpong":
            frames = max(12, int(CHUNK_BYTES / (v["width"] * v["height"] * 1.5)))
            step = frames / v["fps"]
            starts = []
            at = 0.0
            while at < length - 1e-6:
                starts.append(at)
                at += step
            for n, start in enumerate(reversed(starts)):  # the last piece first, each one backwards
                piece = work / f"back{n}.mp4"
                run("-ss", f"{a / 1000 + start:.6f}", "-t", f"{min(step, length - start):.6f}", "-i", str(src),
                    "-vf", "reverse", *encode, str(piece))
                files.append(piece)
        listing = work / "list.txt"
        listing.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
        part = path.with_suffix(f".{os.getpid()}.part.mp4")
        run("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(part))
        part.replace(path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return path, length * (2 if tail == "pingpong" else 1)


# ---- the title

def png(width: int, height: int, rgb: bytes) -> bytes:
    """A PNG of raw rgb24 rows: stdlib only, so ffmpeg can read the title as a picture."""
    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


class TitleLayer:
    """The title as an input of the graph: a picture of its box, looped for as long as it shows, and
    the filters that scale it to the video, wipe it in and place it."""

    def __init__(self, project, size: tuple[int, int], fps: float, seconds: float, folder: Path):
        from .video import Atlas, class_colours, compose, grid, Title, TITLE_FROM, TITLE_UNTIL, TITLE_WIPE, CELL_H, CELL_W
        text, artist = setting(project, "title"), setting(project, "artist")
        self.usable = bool(text or artist)
        if not self.usable:
            return
        cols, rows = grid()
        self.until = min(TITLE_UNTIL, seconds / 2)
        title = Title(text, artist, cols, rows, until=1e9)  # the whole of it; `until` is the overlay's
        if title.box is None:
            self.usable = False
            return
        left, top, right, bottom = title.box
        frame = compose(title.over([], TITLE_FROM + TITLE_WIPE + 1), Atlas(extra=text + artist), class_colours(project.root),
                        cols, rows)
        row_bytes = cols * CELL_W * 3
        pieces = [frame[y * row_bytes + left * CELL_W * 3:y * row_bytes + (right + 1) * CELL_W * 3]
                  for y in range(top * CELL_H, (bottom + 1) * CELL_H)]
        box_w, box_h = (right - left + 1) * CELL_W, (bottom - top + 1) * CELL_H
        self.path = folder / "title.png"
        self.path.write_bytes(png(box_w, box_h, b"".join(pieces)))
        w, h = size
        scale = min(w / (cols * CELL_W), h / (rows * CELL_H))  # the grid's canvas fitted in the video, centred
        self.cells = right - left + 1
        self.width, self.height = max(2, round(box_w * scale)), max(2, round(box_h * scale))
        self.x = (w - cols * CELL_W * scale) / 2 + left * CELL_W * scale
        self.y = (h - rows * CELL_H * scale) / 2 + top * CELL_H * scale
        self.start, self.wipe = TITLE_FROM, TITLE_WIPE
        self.fps = fps

    def args(self) -> list[str]:
        return ["-loop", "1", "-framerate", f"{self.fps:.6f}", "-t", f"{self.until:.6f}", "-i", str(self.path)]

    def text(self, index: int) -> str:
        cell = self.width / self.cells  # wiped in a cell at a time, as the screens do
        return (f"[{index}:v]format=rgba,scale={self.width}:{self.height}:flags=area,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='if(lt(floor(X/{cell:.5f}),round({self.cells}*clip((T-{self.start})/{self.wipe},0,1))),255,0)'[title]")

    def over(self, below: str) -> str:
        return (f"[{below}][title]overlay=x={self.x:.3f}:y={self.y:.3f}:enable='between(t,{self.start},{self.until:.6f})'"
                f":eof_action=pass:format=auto")


# ---- the graph

class Layer:
    """One video track ready to be put into the graph: its input arguments and filter chain."""

    def __init__(self, project, t: dict, number: int, *, fps: float, size: tuple[int, int], mode: str, head_s: float,
                 flow_width: int):
        self.t, self.label = t, f"v{number}"
        v = t["video"]
        a_ms, b_ms = audible(t)
        points = video_points(t)
        first = (points[0][1] - a_ms) / 1000  # picture time (from where the file is read) of the first point
        self.points = [((p / 1000) + head_s, (s - a_ms) / 1000) for p, s in points]
        self.start, self.end = self.points[0][0], self.points[-1][0]
        last = self.points[-1][1]
        self.args: list[str] = []
        margin = MARGIN[mode]
        reach = last + margin
        tail = v["tail"] if v["warp"] else ""
        if tail:
            path, cycle = cycle_file(project, t, tail)
            repeats = max(0, math.ceil(reach / cycle) - 1)
            self.args += ["-stream_loop", str(repeats), "-t", f"{reach:.6f}", "-i", str(path)]
        else:
            self.args += ["-ss", f"{a_ms / 1000:.6f}", "-t", f"{reach:.6f}", "-i", str(project.tracks_dir / t["file"])]
        w, h = fit(v["width"], v["height"], *size)
        chain = []
        if first > 0.0005:
            chain.append(f"trim=start={first:.6f}")
        chain.append(f"setpts='({inverse_expression(self.points)})/TB'")
        if mode == "flow":
            fw = even(min(v["width"], flow_width))
            fh = even(v["height"] * fw / v["width"])
            chain += [f"scale={fw}:{fh}:flags=bicubic",
                      f"minterpolate=fps={fps:.6f}:mi_mode=mci:mc_mode=aobmc:vsbmc=1",
                      f"tpad=stop_mode=clone:stop_duration=0.5",
                      f"scale={w}:{h}:flags=bicubic"]
        else:
            chain.append(f"scale={w}:{h}:flags=bicubic")
            chain.append(f"fps={fps:.6f}:round=near" if mode == "nearest"
                         else f"framerate=fps={fps:.6f}:interp_start=0:interp_end=255:scene=100")
        chain.append(f"trim=end={self.end:.6f}")
        chain.append("format=yuva420p")
        self.chain, self.size = chain, (w, h)

    def fades(self, fade_in: float, fade_out: float, opacity: float, covered_at_end: bool) -> None:
        length = self.end - self.start
        fade_in = min(fade_in, length / 2)
        fade_out = 0.0 if covered_at_end else min(fade_out, length / 2)
        if fade_in > 0.0005:
            self.chain.append(f"fade=t=in:st={self.start:.6f}:d={fade_in:.6f}:alpha=1")
        if fade_out > 0.0005:
            self.chain.append(f"fade=t=out:st={self.end - fade_out:.6f}:d={fade_out:.6f}:alpha=1")
        if opacity < 0.999:
            self.chain.append(f"lut=a='val*{opacity:.4f}'")

    def text(self, index: int) -> str:
        return f"[{index}:v]{','.join(self.chain)}[{self.label}]"


def graph(layers: list[Layer], size: tuple[int, int], fps: float, seconds: float, title: "TitleLayer | None" = None) -> str:
    """The whole filtergraph: black, then each layer over what is below it, then the title. Input i is
    layer i's; the title's is the one after the layers."""
    w, h = size
    lines = [f"color=c=black:s={w}x{h}:r={fps:.6f}:d={seconds:.6f},format=yuv420p[base]"]
    lines += [layer.text(i) for i, layer in enumerate(layers)]
    below = "base"
    for i, layer in enumerate(layers):
        lines.append(f"[{below}][{layer.label}]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass:format=auto[o{i}]")
        below = f"o{i}"
    if title is not None:
        lines.append(title.text(len(layers)))
        lines.append(title.over(below) + "[ot]")
        below = "ot"
    lines.append(f"[{below}]format=yuv420p[out]")
    return ";\n".join(lines) + "\n"


def layers_for(project, videos: list[dict], *, fps: float, size: tuple[int, int], mode: str | None, head_s: float,
               flow_width: int = FLOW_W) -> list[Layer]:
    """Every track as a layer, bottom first, with its fades and opacity. A clip's fade-out is left off
    where a track above it covers its end: the one above fades in over it instead."""
    layers = [Layer(project, t, i, fps=fps, size=size, mode=mode or t["video"]["mode"], head_s=head_s,
                    flow_width=flow_width) for i, t in enumerate(videos)]
    for i, (layer, t) in enumerate(zip(layers, videos)):
        v = t["video"]
        fade_out = v["fade_out_ms"] / 1000
        covered = any(above.start <= layer.end - fade_out + 0.0005 and above.end >= layer.end - 0.0005
                      for above in layers[i + 1:])
        layer.fades(v["fade_in_ms"] / 1000, fade_out, v["opacity"], covered)
    return layers


# ---- running it

def render(project, videos: list[dict], audio: Path, target: Path, *, seconds: float, head_ms: int, fps: float,
           size: tuple[int, int], mode: str | None, preview: bool, flow_width: int = FLOW_W,
           title: bool = False) -> None:
    """Write the mp4: the layers over black, the title on top when asked, audio from `audio`, to
    `target` by way of a part file."""
    layers = layers_for(project, videos, fps=fps, size=size, mode=mode, head_s=head_ms / 1000, flow_width=flow_width)
    frames = math.ceil(seconds * fps)
    folder = Path(tempfile.mkdtemp(prefix="gout-title-"))
    title_layer = TitleLayer(project, size, fps, seconds, folder) if title else None
    if title_layer is not None and not title_layer.usable:
        title_layer = None
    script = folder / "graph.txt"
    script.write_text(graph(layers, size, fps, seconds, title_layer), encoding="utf-8")
    part = target.with_name(target.stem + ".part" + target.suffix)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
    for layer in layers:
        command += layer.args
    if title_layer is not None:
        command += title_layer.args()
    audio_at = len(layers) + (title_layer is not None)
    command += ["-i", str(audio), *graph_from_file(script), "-map", "[out]", "-map", f"{audio_at}:a",
                "-c:v", OUT, "-preset", "veryfast" if preview else "medium", "-crf", "28" if preview else "20",
                "-pix_fmt", "yuv420p", "-r", f"{fps:.6f}", "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
                "-t", f"{seconds:.6f}", "-movflags", "+faststart", str(part)]
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
    progress = Progress(frames)
    done = [0]

    def read() -> None:
        for line in proc.stdout:
            if line.startswith("frame="):
                try:
                    done[0] = int(line.split("=")[1])
                except ValueError:
                    pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            progress.update(min(frames, done[0]))
            try:
                proc.wait(0.25)
            except subprocess.TimeoutExpired:
                pass
        reader.join(2)
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        part.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        progress.close()
        raise
    progress.update(frames)
    progress.close()
    shutil.rmtree(folder, ignore_errors=True)
    if proc.returncode != 0:
        errors.seek(0)
        said = errors.read().decode(errors="replace").strip().splitlines()
        part.unlink(missing_ok=True)
        die("ffmpeg could not write the video: " + (said[-1] if said else "it stopped"))
    errors.close()
    part.replace(target)
