"""Videos of the song, for YouTube and the like: a cover image, or a screen such as the fractal
drawn frame by frame with the music. H.264 and AAC in an mp4.

Text becomes pixels with nothing outside Python and ffmpeg: ffmpeg's drawtext draws every
character gout needs once, in a monospace font, into a glyph atlas; gout pastes those glyphs,
coloured as color.json colours the terminal, onto black frames of 160 by 45 characters, 12 by 24
pixels each (1920 by 1080), and pipes the raw frames into ffmpeg next to master.wav.

A screen (gout video fractal 3 1 0) is drawn by worker processes, `gout _video`, each given every
n-th frame: which choice to show and the moment of the song. They send the rows back as JSON lines,
and gout turns them into pixels in order. A screen switches between its choices every 10 s, at the
strongest drum hit within a second of the mark when there is one.

GOUT_FONT names the font file when the system's monospace font is not the one wanted or cannot be
found. GOUT_VIDEO_GRID=COLSxROWS makes a smaller grid and GOUT_VIDEO_WORKERS=N sets the number of
worker processes (the tests use both).
"""
from __future__ import annotations

import bisect
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .core import detached, die, fmt_ms, gout_command, stop_process
from .theme import DEFAULTS, NAMES, load_theme, parse_color

COLS, ROWS = 160, 45
CELL_W, CELL_H = 12, 24
FPS = 25
EVERY_MS = 10000  # a screen changes its choice this often
SEARCH_MS = 1000  # looking this far either side of the mark for a drum hit
HIT = 0.3  # the onset level (0 .. 1) that counts as a hit
TITLE_FROM, TITLE_UNTIL, TITLE_WIPE = 0.5, 6.5, 0.6  # seconds of video: in from the left, then gone
COVER_SECONDS = 6.0  # --cover shows the image this long, to the nearest drum hit
RAMP = " .:-=+*#%@"  # the fractal's characters, faint to full: the title is drawn in them
BOX = "\x00"  # a cell that is black even over an image
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
        cells = [None if ch == " " else atlas.blank if ch == BOX else atlas.glyph(ch, colours.get(cls, fg))
                 for ch, cls in zip(text, classes)]
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


# ---- screens

def workers() -> int:
    wanted = os.environ.get("GOUT_VIDEO_WORKERS", "")
    if wanted.isdigit() and int(wanted) > 0:
        return int(wanted)
    return max(1, (os.cpu_count() or 2) - 1)


def cut_points(onset: list[float], every_ms: int, end_ms: int, frame_ms: int) -> list[int]:
    """When a screen changes choice, in project ms: every every_ms, moved to the strongest hit within
    SEARCH_MS of the mark when one reaches HIT. Nothing in the last two seconds."""
    cuts: list[int] = []
    target = every_ms
    while target < end_ms - 2000:
        lo = max(target - SEARCH_MS, cuts[-1] + every_ms // 2 if cuts else 0)
        hi = min(end_ms, target + SEARCH_MS)
        window = onset[lo // frame_ms:hi // frame_ms]
        best = max(range(len(window)), key=window.__getitem__) if window else -1
        cuts.append(lo // frame_ms * frame_ms + best * frame_ms if best >= 0 and window[best] >= HIT else target)
        target += every_ms
    return cuts


def schedule(frames: int, fps: int, head_ms: int, cuts: list[int], words: list[str]) -> list[tuple[int, str | None, float]]:
    """(frame, the choice to show, project ms) for every frame of the video."""
    tasks = []
    for frame in range(frames):
        ms = frame * 1000 / fps - head_ms
        word = words[bisect.bisect_right(cuts, ms) % len(words)] if words else None
        tasks.append((frame, word, ms))
    return tasks


class ScreenFrames:
    """The rows of every frame from worker processes, handed out in order. Each worker draws every
    n-th frame and waits while gout catches up, so the rows in memory stay few."""

    def __init__(self, project, screen_name: str, tasks: list, cols: int, rows: int, length_ms: int, count: int,
                 first: int = 0):
        self.count, self.first = count, first
        self.procs, self.queues, self.errors = [], [], []
        for j in range(count):
            errors = tempfile.TemporaryFile()
            proc = subprocess.Popen([*gout_command(), "_video"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=errors, text=True, encoding="utf-8", **detached())
            job = {"project": str(project.root), "screen": screen_name, "cols": cols, "rows": rows,
                   "length_ms": length_ms, "tasks": tasks[j::count]}
            proc.stdin.write(json.dumps(job) + "\n")
            proc.stdin.close()
            q: queue.Queue = queue.Queue(maxsize=4)
            threading.Thread(target=self.listen, args=(proc, q), daemon=True).start()
            self.procs.append(proc)
            self.queues.append(q)
            self.errors.append(errors)

    @staticmethod
    def listen(proc, q) -> None:
        for line in proc.stdout:
            q.put(json.loads(line))
        q.put(None)

    def rows(self, frame: int) -> list:
        worker = (frame - self.first) % self.count
        item = self.queues[worker].get()
        if item is None or item[0] != frame:
            self.stop()
            err = self.errors[worker]
            err.seek(0)
            said = [line for line in err.read().decode(errors="replace").splitlines() if line.strip()]
            die(f"drawing frame {frame} failed: " + (said[-1] if said else "the worker stopped"))
        return item[1]

    def stop(self) -> None:
        for proc in self.procs:
            stop_process(proc)


def worker_main() -> int:
    """gout _video: draw the frames of a job (see ScreenFrames) and print their rows."""
    from .analysis import project_features
    from .project import Project
    from .screens import ScreenContext, screens
    job = json.loads(sys.stdin.readline())
    project = Project(Path(job["project"]))
    screen = screens()[job["screen"]]
    ctx = ScreenContext(project)
    ctx.features = project_features(project)
    ctx.offline, ctx.playing, ctx.length_ms = True, True, job["length_ms"]
    showing = object()
    for frame, word, ms in job["tasks"]:
        if word != showing and word is not None:
            screen.pick(ctx, word)
        showing = word
        ctx.position_ms = ms
        rows = screen.frame(ctx, job["cols"], job["rows"])
        sys.stdout.write(json.dumps([frame, [[text, classes] for text, classes in rows]]) + "\n")
        sys.stdout.flush()
    return 0


# ---- the title

def wrap(words: list[str], width: int) -> list[str]:
    lines: list[str] = []
    for word in words:
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


def split_title(title: str) -> tuple[str, str]:
    """The name to draw big and what follows it after a dash or a colon, to write under it."""
    for mark in (" – ", " — ", " - ", ": "):
        if mark in title:
            name, _, rest = title.partition(mark)
            return name.strip(), rest.strip()
    return title.strip(), ""


class Title:
    """The title drawn big in the fractal's characters (ffmpeg draws it, gout reads the coverage of
    every cell back as one of RAMP), on a black box in the middle of the grid. What follows a dash
    or a colon in the title goes under it in plain characters, and the artist under that."""

    def __init__(self, title: str, artist: str, cols: int, rows: int, font: Path | None = None,
                 until: float = TITLE_UNTIL):
        self.cols, self.rows, self.until = cols, rows, until
        self.cells: dict[tuple[int, int], tuple[str, str]] = {}  # (x, y) -> (character, class)
        name, subtitle = split_title(title)
        big = self.big_text(name, font or find_font()) if name else []
        plain = [(line[:cols - 4], cls) for line, cls in ((subtitle, "m"), (artist.strip(), " ")) if line]
        height = len(big) + (1 if big and plain else 0) + 2 * len(plain) - (1 if plain else 0)
        top = max(1, (rows - height) // 2)
        for y, line in enumerate(big):
            for x, ch in enumerate(line):
                if ch != " ":
                    self.cells[(x, top + y)] = (ch, "m")
        y = top + len(big) + (1 if big else 0)
        for line, cls in plain:
            left = (cols - len(line)) // 2
            for x, ch in enumerate(line):
                self.cells[(left + x, y)] = (ch, cls)
            y += 2
        if self.cells:
            xs, ys = [x for x, _ in self.cells], [y for _, y in self.cells]
            self.box = (max(0, min(xs) - 3), max(0, min(ys) - 1), min(cols - 1, max(xs) + 3), min(rows - 1, max(ys) + 1))
        else:
            self.box = None

    def big_text(self, title: str, font: Path) -> list[str]:
        """The title in RAMP characters, as large as fits two thirds of the grid, in up to three lines."""
        pixels_w, pixels_h = self.cols * 2, self.rows * 4  # two by four pixels a cell: a cell is twice as tall as wide
        size, lines = 8, [title]
        for size in range(64, 7, -2):
            lines = wrap(title.split(), max(1, int(pixels_w * 0.9 / (size * 0.6))))
            if len(lines) <= 3 and max(len(l) for l in lines) * size * 0.6 <= pixels_w * 0.9 \
                    and len(lines) * size * 1.25 <= pixels_h * 0.7:
                break
        stroke = max(1, size // 20)  # thin strokes cover too little of a cell to read at video size
        folder = Path(tempfile.mkdtemp(prefix="gout-title-"))
        try:
            shutil.copy(font, folder / f"font{font.suffix}")
            top = (pixels_h - len(lines) * size * 1.25) / 2
            filters = []
            for i, line in enumerate(lines):
                (folder / f"t{i}.txt").write_text(line, encoding="utf-8")
                filters.append(f"drawtext=fontfile=font{font.suffix}:textfile=t{i}.txt:expansion=none:fontsize={size}"
                               f":fontcolor=white:borderw={stroke}:bordercolor=white:x=(w-tw)/2"
                               f":y={round(top + i * size * 1.25)}:y_align=font")
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s={pixels_w}x{pixels_h}:d=0.04",
                 "-vf", ",".join(filters), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                capture_output=True, cwd=folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        image = result.stdout
        if len(image) != pixels_w * pixels_h:
            return []
        out = []
        for y in range(self.rows):
            line = []
            for x in range(self.cols):
                total = sum(image[(4 * y + dy) * pixels_w + 2 * x + dx] for dy in range(4) for dx in range(2))
                cover = (total / (8 * 255)) ** 0.6  # a cell half covered reads as more than half
                line.append(RAMP[min(len(RAMP) - 1, round(cover * (len(RAMP) - 1)))])
            out.append("".join(line))
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        return out

    def over(self, rows: list, seconds: float) -> list:
        """The rows with the title on them at this moment of the video: wiped in from the left from
        TITLE_FROM, gone at until (TITLE_UNTIL, or earlier in a short song)."""
        if self.box is None or not TITLE_FROM <= seconds < self.until:
            return rows
        left, top, right, bottom = self.box
        shown = left + round((right - left + 1) * min(1.0, (seconds - TITLE_FROM) / TITLE_WIPE))
        out = []
        for y in range(self.rows):
            text, classes = rows[y] if y < len(rows) else ("", "")
            if not top <= y <= bottom:
                out.append((text, classes))
                continue
            text, classes = list(text[:self.cols].ljust(self.cols)), list(classes[:self.cols].ljust(self.cols))
            for x in range(left, min(shown, right + 1)):
                text[x], classes[x] = self.cells.get((x, y), (BOX, " "))
            out.append(("".join(text), "".join(classes)))
        return out
