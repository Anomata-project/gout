"""Videos of the song, for YouTube and the like: a cover image, or a screen such as the fractal
drawn frame by frame with the music. H.264 and AAC in an mp4.

Text becomes pixels with nothing outside Python and ffmpeg: ffmpeg's drawtext draws every
character gout needs once, in a monospace font, into a glyph atlas; gout pastes those glyphs,
coloured as color.json colours the terminal, onto black frames of 160 by 45 characters, 12 by 24
pixels each (1920 by 1080), and pipes the raw frames into ffmpeg next to master.wav.

GOUT_FONT names the font file when the system's monospace font is not the one wanted or cannot be
found. GOUT_VIDEO_GRID=COLSxROWS makes a smaller grid (the tests use it).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .core import die, fmt_ms
from .theme import DEFAULTS, NAMES, load_theme, parse_color

COLS, ROWS = 160, 45
CELL_W, CELL_H = 12, 24
FPS = 25
EXTRA_GLYPHS = "█▓▒░━│·▶■●"  # what gout's own pictures use besides ASCII
FONT_CANDIDATES = {
    "darwin": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf"],
    "win32": ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf"],
    "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
              "/usr/share/fonts/dejavu/DejaVuSansMono.ttf"],
}
DEFAULT_FG = (0xd0, 0xd0, 0xd0)  # a class of " ": the terminal's own foreground
XTERM_BASIC = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
               (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
               (255, 0, 255), (0, 255, 255), (255, 255, 255)]


def grid() -> tuple[int, int]:
    wanted = os.environ.get("GOUT_VIDEO_GRID", "")
    if wanted:
        try:
            cols, rows = (int(v) for v in wanted.lower().split("x"))
            if cols > 0 and rows > 0:
                return cols, rows
        except ValueError:
            pass
        die(f"GOUT_VIDEO_GRID={wanted}: use COLSxROWS, like 40x12")
    return COLS, ROWS


def find_font() -> Path:
    wanted = os.environ.get("GOUT_FONT", "").strip()
    if wanted:
        if not Path(wanted).is_file():
            die(f"GOUT_FONT={wanted}: no such file")
        return Path(wanted)
    if shutil.which("fc-match"):
        found = subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True).stdout.strip()
        if found and Path(found).is_file():
            return Path(found)
    platform = "darwin" if sys.platform == "darwin" else "win32" if os.name == "nt" else "linux"
    for candidate in FONT_CANDIDATES[platform]:
        if Path(candidate).is_file():
            return Path(candidate)
    die("no monospace font found to draw the characters with: GOUT_FONT=/path/to/a/monospace.ttf")


# ---- colours

def xterm_rgb(index: int) -> tuple[int, int, int]:
    if index < 16:
        return XTERM_BASIC[index]
    if index < 232:
        steps = (0, 95, 135, 175, 215, 255)
        index -= 16
        return steps[index // 36], steps[index // 6 % 6], steps[index % 6]
    grey = 8 + 10 * (index - 232)
    return grey, grey, grey


def rgb(value, fallback: tuple[int, int, int] = DEFAULT_FG) -> tuple[int, int, int]:
    """The foreground of a colour.json value (a colour or a style object) as red, green, blue."""
    if isinstance(value, dict):
        value = value.get("fg", "default")
    colour = parse_color(value)
    if isinstance(colour, tuple):
        return colour
    if isinstance(colour, int) and colour >= 0:
        return xterm_rgb(colour)
    if isinstance(value, str) and value.strip().lower() in NAMES:
        return xterm_rgb(NAMES[value.strip().lower()])
    return fallback


def class_colours(project_root: Path | None) -> dict[str, tuple[int, int, int]]:
    """A cell class to its colour, as the ui has it: digits are the track palette, letters roles."""
    theme, _, _ = load_theme(project_root)
    palette = theme.get("track_palette") or DEFAULTS["track_palette"]
    colours = {" ": DEFAULT_FG}
    for digit in range(10):
        colours[str(digit)] = rgb(palette[digit % len(palette)])
    roles = {"m": "master_wave", "s": "muted_wave", "t": "trimmed_wave", "c": "center_line", "r": "ruler",
             "l": "ruler_labels", "p": "playhead", "g": "gap_line", "a": "effect_curve", "z": "center_line",
             "x": "trimmed_wave", "h": "cheat_heading", "e": "error", "k": "prompt"}
    for cls, role in roles.items():
        colours[cls] = rgb(theme.get(role, DEFAULTS.get(role)))
    return colours


# ---- characters

class Atlas:
    """Every character gout draws, once, as 8-bit coverage from ffmpeg's drawtext; glyphs in a colour
    are made on first use and kept."""

    def __init__(self, cell_w: int = CELL_W, cell_h: int = CELL_H, font: Path | None = None):
        self.cell_w, self.cell_h = cell_w, cell_h
        self.chars = [chr(c) for c in range(32, 127)] + list(EXTRA_GLYPHS)
        self.index = {c: i for i, c in enumerate(self.chars)}
        self.coverage = self.draw(font or find_font())
        self.cache: dict[tuple[str, tuple[int, int, int]], list[bytes]] = {}
        self.blank = [bytes(3 * cell_w)] * cell_h

    def draw(self, font: Path) -> bytes:
        size = max(6, min(round(self.cell_w / 0.6), round(self.cell_h / 1.25)))
        baseline = round(self.cell_h * 0.79)
        folder = Path(tempfile.mkdtemp(prefix="gout-glyphs-"))
        try:  # files by plain relative names keep drawtext's option escaping out of it (and C: paths)
            shutil.copy(font, folder / f"font{font.suffix}")
            filters = []
            for i, c in enumerate(self.chars):
                (folder / f"g{i}.txt").write_text(c, encoding="utf-8")
                filters.append(f"drawtext=fontfile=font{font.suffix}:textfile=g{i}.txt:expansion=none:fontsize={size}"
                               f":fontcolor=white:x={i * self.cell_w}+({self.cell_w}-tw)/2:y={baseline}:y_align=baseline")
            width = len(self.chars) * self.cell_w
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"color=c=black:s={width}x{self.cell_h}:d=0.04", "-vf", ",".join(filters), "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, cwd=folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if result.returncode != 0 or len(result.stdout) != width * self.cell_h:
            problem = result.stderr.decode(errors="replace").strip().splitlines()
            die("ffmpeg could not draw the characters (it needs drawtext, built with libfreetype): "
                + (problem[-1] if problem else "no picture"))
        return result.stdout

    def glyph(self, char: str, colour: tuple[int, int, int]) -> list[bytes]:
        """The character's pixel rows in rgb24; a character the atlas lacks is drawn as ?."""
        key = (char, colour)
        rows = self.cache.get(key)
        if rows is None:
            i = self.index.get(char, self.index["?"])
            width = len(self.chars) * self.cell_w
            r, g, b = colour
            rows = []
            for y in range(self.cell_h):
                start = y * width + i * self.cell_w
                rows.append(bytes(v for a in self.coverage[start:start + self.cell_w]
                                  for v in (r * a // 255, g * a // 255, b * a // 255)))
            if not any(any(row) for row in rows):
                rows = self.blank
            self.cache[key] = rows
        return rows


def compose(rows: list[tuple[str, str]], atlas: Atlas, colours: dict, cols: int, rows_n: int,
            background: bytes | None = None) -> bytes:
    """A raw rgb24 frame of cols by rows_n cells from a screen's rows of (text, classes). Blank
    cells show the background frame when there is one, black otherwise."""
    lines: list[bytes] = []
    blank_row = bytes(3 * atlas.cell_w * cols)
    fg = colours.get(" ", DEFAULT_FG)
    for y in range(rows_n):
        text, classes = rows[y] if y < len(rows) else ("", "")
        text = text[:cols].ljust(cols)
        classes = classes[:cols].ljust(cols)
        if not text.strip():
            if background is None:
                lines.extend([blank_row] * atlas.cell_h)
            else:
                start = y * atlas.cell_h * len(blank_row)
                lines.append(background[start:start + atlas.cell_h * len(blank_row)])
            continue
        cells = [atlas.glyph(ch, colours.get(cls, fg)) if ch != " " else None for ch, cls in zip(text, classes)]
        for py in range(atlas.cell_h):
            if background is None:
                lines.append(b"".join(atlas.blank[py] if c is None else c[py] for c in cells))
            else:
                offset = (y * atlas.cell_h + py) * len(blank_row)
                back = background[offset:offset + len(blank_row)]
                step = 3 * atlas.cell_w
                lines.append(b"".join(back[x * step:(x + 1) * step] if c is None else c[py]
                                      for x, c in enumerate(cells)))
    return b"".join(lines)


def picture(path: Path, width: int, height: int) -> bytes:
    """An image as a raw rgb24 frame, scaled to fit with black bars."""
    if not path.is_file():
        die(f"no such image: {path}")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-vf",
         f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
         "format=rgb24", "-f", "rawvideo", "-"], capture_output=True)
    if result.returncode != 0 or len(result.stdout) != width * height * 3:
        die(f"cannot read the image {path}: {result.stderr.decode(errors='replace').strip()[-200:]}")
    return result.stdout


# ---- the file

class Encoder:
    """ffmpeg taking raw frames on stdin and the audio from a file, into an mp4 written as a part
    file and renamed when it is complete."""

    def __init__(self, out: Path, audio: Path, width: int, height: int, fps: int = FPS):
        self.out = out
        self.part = out.with_name(out.stem + ".part" + out.suffix)
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
             "-i", str(audio), "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-movflags", "+faststart", str(self.part)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errors)

    def write(self, frame: bytes) -> None:
        try:
            self.proc.stdin.write(frame)
        except (BrokenPipeError, OSError):
            self.fail()

    def fail(self) -> None:
        self.errors.seek(0)
        said = self.errors.read().decode(errors="replace").strip().splitlines()
        self.abort()
        die("ffmpeg could not write the video: " + (said[-1] if said else "it stopped"))

    def finish(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.wait() != 0:
            self.fail()
        self.part.replace(self.out)

    def abort(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.kill()
        self.proc.wait()
        self.part.unlink(missing_ok=True)


class Progress:
    """A line that counts frames and the time left, when stdout is a terminal."""

    def __init__(self, total: int):
        self.total, self.started, self.shown = total, time.monotonic(), 0.0
        self.live = sys.stdout.isatty()

    def update(self, done: int) -> None:
        now = time.monotonic()
        if not self.live or (now - self.shown < 0.5 and done < self.total):
            return
        self.shown = now
        left = (now - self.started) / max(1, done) * (self.total - done)
        print(f"\r      frame {done}/{self.total}  {100 * done // max(1, self.total)}%  {fmt_ms(left * 1000)} left  ",
              end="", flush=True)

    def close(self) -> None:
        if self.live:
            print()
