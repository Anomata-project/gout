"""gout web: the preview of gout in a browser. No login; one project per browser.

    gout web [--host 127.0.0.1] [--port 8321] [--root DIR] [--downloads DIR] [--releases OWNER/REPO]
             [--trust-proxy] [-q]

--releases names the GitHub repository whose latest release holds the installers the page offers.

The page (gout/webpage/) talks to a small JSON api under /api/. The first visit makes a project
and the page keeps its key in localStorage; the key comes back in the X-Gout-Key header, and only
its sha256 names the project's folder under the root, so the folders do not give keys away.

The limits are the server's, not the page's: MAX_FILES files of mp3 or wav, MAX_FILE_BYTES each,
a song of up to MAX_SONG_MS, RENDERS renders at a time over the whole server, projects deleted
after DAYS unused, NEW_PER_HOUR new projects per address, MAX_PROJECTS in all. The mix comes out
as mp3 only; master.wav stays on the server (16 bit) for the timeline and is never served.

Commands run as `gout -p PROJECT ...` in their own process, without addons, so a slow or broken
command never takes the server along and two visitors' output never mixes. Only commands that
stay inside the project get through (SERVER_COMMANDS and the effects): no file paths, no -p, no
-v (it prints the server's paths). The page handles its own words (play, stop, view, fractal...).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import analysis
from .cli import aliases
from .core import __version__, DB_NAME, DEFAULT_RATE, gout_command, GoutError, MASTER_MP3, MASTER_N, MASTER_WAV, \
    TRACK_DIR
from .commands import chain_kinds, panel_target, settled_panel
from .formula import FormulaError, parse
from .fx import effect, effects, GUTTER, resolve
from .media import probe
from .mixer import sounding_end
from .project import Project
from .render import CHEAT_HEADINGS, cheat_layout, cheat_sections, LABEL_W, panel_head, render_panel, \
    render_timeline, timeline_span
from .settings import master_track, setting
from .theme import BASIC_RGB, CUBE, DEFAULTS, NAMES, rgb_of

PAGE_DIR = Path(__file__).resolve().parent / "webpage"

MAX_FILES = 5
MAX_FILE_BYTES = 20_000_000
DAYS = 7
MAX_SONG_MS = 10 * 60 * 1000
RENDERS = 2
NEW_PER_HOUR = 20
MAX_PROJECTS = 500
COMMAND_SECONDS = 300
JSON_LIMIT = 64_000
PANEL_HEIGHT = 8
LIVE_NOTE = "not rendered yet: space renders it and plays"

KEY_HEADER = "X-Gout-Key"
KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}")    # secrets.token_urlsafe(32)
USED_FILE = ".gout/web-used"                   # its mtime: when the project was last used
UPLOAD_DIR = ".gout/upload"
FORMATS = {".mp3": lambda codec: codec == "mp3", ".wav": lambda codec: codec.startswith("pcm_")}
ZIP_FOLDER = "gout-preview"
PREVIEW_SETTINGS = {"autorender": "idle", "bits": None, "name": ZIP_FOLDER}  # in the zip: gout's defaults again

SERVER_COMMANDS = {"ls", "move", "trim", "rm", "mute", "solo", "gain", "pan", "fx", "set", "stats", "undo", "dump"}
NOT_IN_PREVIEW = {
    "add": "upload files with the + button under the prompt (mp3 or wav, up to 5)",
    "scan": "upload files with the + button under the prompt",
    "mix": "space plays the mix (the page renders it first); download it as mp3 under the files",
}
DOWNLOAD_ONLY = {"saveas", "stems", "import", "new", "rebuild", "ui", "cut", "addons", "colors", "web", "version"}
PAGE_WORDS = {"play", "stop", "view", "cheat", "help", "clear", "fractal", "undo"}  # the page's own, for tab
REFUSED_WORDS = {"-p", "--project", "-v", "--verbose"}
FIXED_SETTINGS = {"autorender": "the preview renders when you play", "automix": "the preview renders when you play",
                  "bits": "the preview gives an mp3; set mp3 320k or v0 chooses its quality"}

ZIP_README = f"""A gout project from the web preview.

Open it with gout (the download on the page you came from; needs ffmpeg):

    gout -p {ZIP_FOLDER}            the terminal ui
    gout -p {ZIP_FOLDER} mix -3     render master.wav and master.mp3

The project is this folder: gout.db (the state and the undo history), gout.json (the same state,
readable) and master/ (your files). The mix is not included; mix renders it again.
"""


class Refused(Exception):
    """A request the preview says no to: an http status and a message for the log."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# ---------------------------------------------------------------------------- commands

def run_gout(root: Path, argv: list[str], timeout: int = COMMAND_SECONDS) -> tuple[bool, list[str]]:
    """Run one gout command on the project in its own process. (ok, output lines), with the
    project's path taken out of the output."""
    env = dict(os.environ, GOUT_NO_ADDONS="1", GOUT_PLAYER="null", XDG_CONFIG_HOME=str(root / ".gout" / "config"))
    env.pop("GOUT_ADDONS", None)
    try:
        result = subprocess.run([*gout_command(), "-p", str(root), *argv], capture_output=True,
                                text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL, cwd=root)
    except subprocess.TimeoutExpired:
        return False, [f"error: {argv[0]} took longer than {timeout} s and was stopped"]
    text = (result.stdout + result.stderr).replace(str(root) + os.sep, "").replace(str(root), ".")
    lines = []
    for line in text.rstrip("\n").splitlines():
        lines.append("error: " + line[len("gout: "):] if line.startswith("gout: ") else line)
    return result.returncode == 0, lines


def web_argv(line: str) -> list[str]:
    """The command line as gout argv when the preview runs it; Refused (400) with the reason if not."""
    try:
        argv = shlex.split(line)
    except ValueError as exc:
        raise Refused(400, f"error: {exc}")
    if not argv:
        raise Refused(400, "error: nothing to run")
    head = aliases().get(argv[0], argv[0])
    if head in NOT_IN_PREVIEW:
        raise Refused(400, f"{head}: {NOT_IN_PREVIEW[head]}")
    if head in DOWNLOAD_ONLY:
        raise Refused(400, f"{head}: not in the web preview; download gout for it (the buttons at the bottom)")
    if head not in SERVER_COMMANDS and resolve(head) is None:
        raise Refused(400, f"error: unknown command {argv[0]!r} — help lists what the preview has")
    for word in argv[1:]:
        if word in REFUSED_WORDS or word.startswith(("--project=", "-p=")):
            raise Refused(400, f"error: {word} is not available in the web preview")
    if head == "set" and len(argv) > 2 and argv[1].lower() in FIXED_SETTINGS:
        raise Refused(400, f"set {argv[1]}: fixed in the web preview ({FIXED_SETTINGS[argv[1].lower()]})")
    argv = [head, *argv[1:]]
    if head == "rm" and not any(w in ("-D", "--delete") for w in argv):
        argv.append("-D")  # the preview keeps no files its tracks do not use: the five are the five
    return argv


# ---------------------------------------------------------------------------- projects

class Store:
    """The projects on disk, their locks, and the limits that span them."""

    def __init__(self, root: Path, days: float = DAYS, max_projects: int = MAX_PROJECTS,
                 new_per_hour: int = NEW_PER_HOUR):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.days, self.max_projects, self.new_per_hour = days, max_projects, new_per_hour
        self.guard = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.renders = threading.BoundedSemaphore(RENDERS)
        self.created: dict[str, deque] = {}

    def folder(self, key: str | None) -> Path | None:
        """The project folder for a key, or None when the key is malformed or unknown."""
        if not key or not KEY_RE.fullmatch(key):
            return None
        path = self.root / hashlib.sha256(key.encode()).hexdigest()[:40]
        return path if (path / DB_NAME).is_file() else None

    def lock(self, path: Path) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(path.name, threading.Lock())

    def projects(self) -> list[Path]:
        return [p for p in self.root.iterdir() if p.is_dir() and re.fullmatch(r"[0-9a-f]{40}", p.name)]

    def create(self, address: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self.guard:
            recent = self.created.setdefault(address, deque())
            while recent and recent[0] < now - 3600:
                recent.popleft()
            if len(recent) >= self.new_per_hour:
                raise Refused(429, "too many new projects from here in the last hour; try again later")
            if len(self.projects()) >= self.max_projects:
                raise Refused(503, "the preview is full right now; try again later, or download gout")
            recent.append(now)
        key = secrets.token_urlsafe(32)
        path = self.root / hashlib.sha256(key.encode()).hexdigest()[:40]
        project = Project.create(path, DEFAULT_RATE)
        try:
            project.set("name", "preview")
            project.set("autorender", "off")  # the page asks for a render when it plays
            project.set("bits", "16")         # master.wav only feeds the timeline and the mp3
            project.sync_json()
        finally:
            project.conn.close()
        self.touch(path)
        return key

    def touch(self, path: Path) -> None:
        used = path / USED_FILE
        try:
            if time.time() - used.stat().st_mtime < 60:
                return
        except OSError:
            used.parent.mkdir(parents=True, exist_ok=True)
        used.touch()

    def expires(self, path: Path) -> float:
        try:
            return (path / USED_FILE).stat().st_mtime + self.days * 86400
        except OSError:
            return time.time() + self.days * 86400

    def sweep(self, now: float | None = None) -> list[str]:
        """Delete projects unused for `days`. The folder names it deleted."""
        now = time.time() if now is None else now
        gone = []
        for path in self.projects():
            used = path / USED_FILE
            try:
                last = used.stat().st_mtime if used.exists() else path.stat().st_mtime
            except OSError:
                continue
            if now - last < self.days * 86400:
                continue
            lock = self.lock(path)
            if not lock.acquire(blocking=False):
                continue  # in use right now, so not unused
            try:
                shutil.rmtree(path, ignore_errors=True)
                gone.append(path.name)
            finally:
                lock.release()
                with self.guard:
                    self.locks.pop(path.name, None)
        return gone

    def delete(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        with self.guard:
            self.locks.pop(path.name, None)


def track_files(project: Project) -> list[Path]:
    if not project.tracks_dir.is_dir():
        return []
    return sorted(f for f in project.tracks_dir.iterdir() if f.is_file() and ".part" not in f.name)


def mp3_current(project: Project) -> bool:
    return (project.root / MASTER_MP3).exists() and project.master_is_current()


def forget_mix(project: Project) -> None:
    """Remove the renders and their analysis: they still hold audio from a removed file."""
    for name in (MASTER_WAV, MASTER_MP3):
        (project.root / name).unlink(missing_ok=True)
    shutil.rmtree(project.root / analysis.CACHE_DIR, ignore_errors=True)


def song_end_ms(project: Project) -> int:
    tracks = project.tracks()
    if not tracks:
        return 0
    return max(sounding_end(project, t) for t in tracks) + int(setting(project, "head")) + int(setting(project, "tail"))


# ---------------------------------------------------------------------------- what the page shows

def page_state(project: Project, width: int, panel: tuple[int, str] | None, pictures: bool, store: Store) -> dict:
    width = max(40, min(400, width))
    tracks = project.tracks()
    uses = {t["file"]: t["n"] for t in tracks}
    state = {
        "name": project.get("name"),
        "rate": project.rate,
        "tracks": [{"n": t["n"], "name": t["name"], "file": t["file"], "length_ms": t["length_ms"]} for t in tracks],
        "files": [{"name": f.name, "bytes": f.stat().st_size, "track": uses.get(f.name)} for f in track_files(project)],
        "mix": {"current": mp3_current(project), "state": project.get("master_state"),
                "head_ms": int(setting(project, "head"))},
        "expires": store.expires(project.root),
        "timeline": {"rows": [[label, LIVE_NOTE if kind == "note" and cells.endswith("plays the project live") else cells,
                               kind, classes, role]
                              for label, cells, kind, classes, role in render_timeline(project, width, styled=True,
                                                                                      theme=THEME)]},
        "panel": None,
        "words": sorted(t["name"] for t in tracks),
    }
    if tracks:
        t0, t1, columns = timeline_span(project, tracks, width)
        state["timeline"].update(t0=t0, t1=t1, columns=columns, label=LABEL_W + 1)
    if panel is not None and effect(panel[1]) is not None:
        n, kind = panel
        track = master_track(project) if n == MASTER_N else next((t for t in tracks if t["n"] == n), None)
        if track is not None:
            rows = (render_panel(project, track, width - 1, PANEL_HEIGHT, kind) if pictures
                    else [(panel_head(track, kind), "", "head")])
            state["panel"] = {"track": n, "kind": kind, "gutter": GUTTER,
                              "who": "master" if n == MASTER_N else f"{track['n']} {track['name']}",
                              "rows": [list(r) for r in rows]}
    return state


WEB_DROP = {"add", "scan", "mix", "saveas", "stems", "import", "rebuild", "new", "quit", "split", "sheet", "colors",
            "addons", "bits", "autorender", "web"}
WEB_ROWS = {
    "rm": ("rm", "r", "TRACK", "remove the track and delete its file"),
    "play": ("play", "pl", "[FROM]", "hear the mix; the page renders it first"),
    "view": ("view", "v", "", "the timeline on and off"),
    "undo": ("undo", "u", "", "undo the last change, again for the one before (ctrl-u); not past a hard trim or rm"),
}
WEB_KEY_SECTIONS = [
    ("FILES", "", [
        ("+", "upload mp3 or wav: up to 5 files of 20 MB; each becomes a track"),
        ("✕", "remove a file and its track"),
    ]),
    ("LINE", "", [
        ("← → home end", "move in the line"),
        ("↑ ↓", "earlier commands, to change and run again"),
        ("tab", "complete a command or track name"),
        ("esc", "clear the line"),
    ]),
    ("PLAY", "", [
        ("space", "on an empty line: play and stop; the playhead stays"),
        ("← →", "on an empty line: move the playhead 5 s"),
        ("stop", "at the prompt: the playhead back to the start"),
        ("▶ play", "the big button at the bottom: play with the fractal, full screen"),
    ]),
    ("KEYS", "", [
        ("ctrl-u", "undo the last change"),
        ("alt-t", "timeline on and off (the browser keeps ctrl-t)"),
        ("ctrl-k", "cheat sheet on and off"),
        ("ctrl-g", "effect pictures on and off (eq N, comp N pick the track)"),
        ("ctrl-l", "clear the log"),
    ]),
    ("FRACTAL", "full screen", [
        ("ctrl-space  fractal", "a Newton fractal moving with the music"),
        ("fractal 5 | rings", "open it with a preset"),
        ("fractal z^5 - 3z + 1", "or a formula of your own"),
        ("fractal presets", "list the presets"),
        ("esc", "back to gout; the song keeps playing"),
        ("space  ← →", "play and stop, move 5 s"),
        ("1 .. 9 0  ↑ ↓", "pick a preset"),
        ("+ -", "zoom in and out"),
        ("c", "colours on and off"),
    ]),
]


def web_sections() -> list[tuple]:
    """The cheat sheet's sections for the preview: what it can run, and its own keys."""
    out = []
    for heading, note, kind, rows in cheat_sections():
        if kind != "commands":
            continue
        kept = [WEB_ROWS.get(r[0], r) for r in rows if r[0] not in WEB_DROP]
        out.append((heading, note, kind, list(dict.fromkeys(kept))))
    return out + [(heading, note, "keys", rows) for heading, note, rows in WEB_KEY_SECTIONS]


def cheat_payload(width: int) -> dict:
    lines, second = cheat_layout(max(30, min(300, width)), web_sections())
    headings = sorted(CHEAT_HEADINGS | {h for h, _, _ in WEB_KEY_SECTIONS})
    return {"lines": lines, "second": second, "headings": headings}


def css_color(value) -> str | None:
    if isinstance(value, str):
        rgb = rgb_of(value)
        if rgb is None and value.lower() in NAMES:
            rgb = BASIC_RGB[NAMES[value.lower()]]
    elif isinstance(value, int) and 0 <= value < 256:
        if value < 16:
            rgb = BASIC_RGB[value]
        elif value < 232:
            i = value - 16
            rgb = (CUBE[i // 36], CUBE[i // 6 % 6], CUBE[i % 6])
        else:
            rgb = (8 + 10 * (value - 232),) * 3
    else:
        return None
    return None if rgb is None else "#%02x%02x%02x" % rgb


def css_rule(selector: str, value) -> str:
    style = value if isinstance(value, dict) else {"fg": value}
    parts = []
    fg, bg = css_color(style.get("fg")), css_color(style.get("bg"))
    if fg:
        parts.append(f"color:{fg}")
    if bg:
        parts.append(f"background:{bg}")
    if style.get("bold"):
        parts.append("font-weight:bold")
    if style.get("dim"):
        parts.append("opacity:.6")
    if style.get("underline"):
        parts.append("text-decoration:underline")
    return f"{selector}{{{';'.join(parts)}}}"


THEME = dict(DEFAULTS)


def theme_css() -> str:
    """color.json's defaults as css: .r-ROLE for a theme role, .c-X for a timeline cell class."""
    from .tui import Tui  # the class letters and their roles live with the ui that drew them first
    rules = [css_rule(f".r-{role}", value) for role, value in THEME.items()
             if role != "track_palette" and (isinstance(value, dict) or css_color(value))]
    rules += [css_rule(f".c-{cls}", THEME[role]) for cls, role in Tui.CLASS_ROLES.items()]
    palette = THEME["track_palette"]
    rules += [css_rule(f".c-{d}", {"fg": palette[d % len(palette)], "bold": True}) for d in range(10)]
    rules.append(":root{" + ";".join(f"--track-{i}:{css_color(c)}" for i, c in enumerate(palette)) + "}")
    return "\n".join(rules) + "\n"


# ---------------------------------------------------------------------------- the installers

RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
INSTALLERS = {  # which file of a release is which system's installer, as packaging/build.py names them
    "windows": re.compile(r"-windows-x64-setup\.exe$"),
    "macos-arm64": re.compile(r"-macos-arm64\.pkg$"),
    "macos-x86_64": re.compile(r"-macos-x86_64\.pkg$"),
    "linux": re.compile(r"_all\.deb$"),
}


class Releases:
    """The installers of the latest release on GitHub, looked up when the server starts and then
    hourly: the page may only talk to its own server, so the server asks GitHub for it."""

    def __init__(self, repo: str | None):
        self.repo = repo
        self.found: dict = {"page": f"https://github.com/{repo}/releases/latest"} if repo else {}

    def refresh(self) -> None:
        if not self.repo:
            return
        request = urllib.request.Request(RELEASES_API.format(repo=self.repo),
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": "gout-web"})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                data = json.load(reply)
        except (OSError, ValueError):
            return  # keep what was found before; there may be no release yet
        found = {"page": data.get("html_url") or self.found.get("page"), "version": data.get("tag_name", "")}
        for asset in data.get("assets") or []:
            for system, pattern in INSTALLERS.items():
                if pattern.search(asset.get("name", "")):
                    found[system] = {"name": asset["name"], "url": asset["browser_download_url"],
                                     "bytes": asset.get("size", 0)}
        self.found = found


# ---------------------------------------------------------------------------- the api

class App:
    def __init__(self, store: Store, downloads: Path | None = None, releases: Releases | None = None):
        self.store = store
        self.downloads = downloads.expanduser().resolve() if downloads else None
        self.releases = releases or Releases(None)

    def info(self) -> dict:
        found = {}
        if self.downloads and self.downloads.is_dir():
            found = {f.name: f"downloads/{f.name}" for f in sorted(self.downloads.iterdir()) if f.is_file()}
        words = set(SERVER_COMMANDS) | PAGE_WORDS
        kinds = {}
        for eff in effects().values():
            words |= {eff.name, *eff.aliases, *eff.shortcuts}
            kinds |= {word: eff.name for word in (eff.name, *eff.aliases)}
        known = words | {"fz", "quit", "exit", "q", "sheet", "split", "timeline"}
        short = {alias: name for alias, name in aliases().items() if name in known}
        return {"version": __version__, "downloads": found, "installers": self.releases.found,
                "commands": sorted(words), "aliases": short,
                "effects": kinds,
                "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES, "days": self.store.days,
                           "song_ms": MAX_SONG_MS, "formats": sorted(FORMATS)}}

    def run(self, root: Path, body: dict) -> dict:
        """One command line: its output, where the effect panel goes now, and the new state."""
        line = str(body.get("line", ""))[:2000]
        try:
            argv = web_argv(line)
        except Refused as exc:
            return {"ok": False, "lines": [exc.message]} | self.state(root, body)
        project = Project(root)
        try:
            before = chain_kinds(project)
        finally:
            project.conn.close()
        ok, lines = run_gout(root, argv)
        panel = body.get("panel") or {}
        track, kind = panel.get("track"), str(panel.get("kind") or "eq")
        project = Project(root)
        try:
            track, kind = settled_panel(before, chain_kinds(project), track if isinstance(track, int) else None, kind)
            if argv[0] == "fx" or resolve(argv[0]) is not None:
                target = panel_target(project, argv[0], argv)
                if target:
                    track, kind = target
        finally:
            project.conn.close()
        body = dict(body, panel={"track": track, "kind": kind} if track is not None else None)
        return {"ok": ok, "lines": lines} | self.state(root, body)

    def state(self, root: Path, body: dict) -> dict:
        panel = body.get("panel") or None
        target = None
        if isinstance(panel, dict) and isinstance(panel.get("track"), int) and panel.get("kind"):
            target = (panel["track"], str(panel["kind"]))
        project = Project(root)
        try:
            return {"state": page_state(project, int(body.get("width") or 100), target,
                                        bool(body.get("pictures", True)), self.store)}
        finally:
            project.conn.close()

    def upload(self, root: Path, name: str, size: int, stream) -> dict:
        """A file into the project as a track. `stream` is read only once the checks pass."""
        name = re.split(r"[\\/]", name)[-1].strip()
        suffix = Path(name).suffix.lower()
        if suffix not in FORMATS:
            raise Refused(415, f"upload: {name or 'that file'} is not mp3 or wav")
        if size > MAX_FILE_BYTES:
            raise Refused(413, f"upload: {name} is {size / 1e6:.1f} MB; the preview takes files up to "
                               f"{MAX_FILE_BYTES // 1_000_000} MB")
        project = Project(root)
        try:
            count = len(track_files(project))
        finally:
            project.conn.close()
        if count >= MAX_FILES:
            raise Refused(409, f"upload: the preview holds {MAX_FILES} files; remove one first")
        folder = root / UPLOAD_DIR
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{secrets.token_hex(8)}{suffix}"
        try:
            left = size
            with open(tmp, "wb") as out:
                while left > 0:
                    chunk = stream.read(min(1 << 16, left))
                    if not chunk:
                        raise Refused(400, "upload: the file arrived incomplete")
                    out.write(chunk)
                    left -= len(chunk)
            try:
                info = probe(tmp)
            except GoutError:
                raise Refused(415, f"upload: {name} is not audio ffmpeg can read")
            if not FORMATS[suffix](info["codec"]):
                raise Refused(415, f"upload: {name} holds {info['codec']}, not {suffix[1:]}")
            stem = Path(name).stem or "track"
            ok, lines = run_gout(root, ["add", str(tmp), "-n", stem])
            lines = [line.replace(f"{UPLOAD_DIR}/{tmp.name}", name) for line in lines]
            if not ok:
                raise Refused(400, "\n".join(lines) or "upload: gout could not add it")
            return {"ok": True, "lines": lines}
        finally:
            tmp.unlink(missing_ok=True)

    def remove(self, root: Path, name: str) -> dict:
        project = Project(root)
        try:
            files = {f.name: f for f in track_files(project)}
            if name not in files:
                raise Refused(404, f"remove: no file {name!r} in the project")
            track = next((t for t in project.tracks() if t["file"] == name), None)
        finally:
            project.conn.close()
        lines: list[str] = []
        if track is not None:
            ok, lines = run_gout(root, ["rm", str(track["n"]), "-D"])
            if not ok:
                raise Refused(400, "\n".join(lines))
        files[name].unlink(missing_ok=True)
        project = Project(root)
        try:
            forget_mix(project)
            project.forget_envelope(name)
        finally:
            project.conn.close()
        return {"ok": True, "lines": lines or [f"rm    {name}  (deleted)"]}

    def mix(self, root: Path) -> tuple[Path, int]:
        """The mix as mp3, rendered first when out of date: (path, head padding in ms)."""
        project = Project(root)
        try:
            if not project.tracks():
                raise Refused(409, "play: no tracks yet — upload a file with the + button")
            current = mp3_current(project)
            end = song_end_ms(project)
            head = int(setting(project, "head"))
        finally:
            project.conn.close()
        if not current:
            if end > MAX_SONG_MS:
                raise Refused(409, f"play: the song runs to {end // 60000}:{end // 1000 % 60:02d}; the preview "
                                   f"renders up to {MAX_SONG_MS // 60000} minutes")
            with self.store.renders:
                ok, lines = run_gout(root, ["mix", "-3"])
            if not ok or not (root / MASTER_MP3).exists():
                raise Refused(409, "\n".join(lines) or "play: nothing to play")
        return root / MASTER_MP3, head

    def mix_state(self, root: Path) -> str:
        project = Project(root)
        try:
            return project.get("master_state") or ""
        finally:
            project.conn.close()

    def features(self, root: Path) -> dict:
        project = Project(root)
        try:
            plan = analysis.plan(project)
        finally:
            project.conn.close()
        found = analysis.compute(plan)
        bands = {name: base64.b64encode(bytes(min(255, max(0, round(v * 255))) for v in found.values[name])).decode()
                 for name in analysis.Features.NAMES}
        return {"frame_ms": analysis.FRAME_MS, "bands": bands}

    def zip_project(self, root: Path) -> Path:
        """The project as a zip: gout.db, gout.json and master/, no renders or caches. The copy gets
        gout's own defaults back for what the preview fixes (autorender, bits), undo history included."""
        work = root / ".gout" / "export"
        shutil.rmtree(work, ignore_errors=True)
        (work / TRACK_DIR).mkdir(parents=True)
        out = root / ".gout" / "export.zip"
        source, copy = sqlite3.connect(root / DB_NAME), sqlite3.connect(work / DB_NAME)
        try:
            source.backup(copy)  # a consistent copy, whatever sqlite has open
        finally:
            copy.close()
            source.close()
        def restore(settings: dict) -> None:
            for key, value in PREVIEW_SETTINGS.items():
                if value is None:
                    settings.pop(key, None)
                else:
                    settings[key] = value

        project = Project(work)
        try:
            for key, value in PREVIEW_SETTINGS.items():
                if value is None:
                    project.unset(key)
                else:
                    project.set(key, value)
            for key in ("master_ms", "master_lufs", "master_tp", "master_lra", "master_state", "master_norm_db"):
                project.unset(key)
            with project.conn:
                for row in project.conn.execute("SELECT id, snapshot FROM history").fetchall():
                    snap = json.loads(row["snapshot"])
                    restore(snap["project"])  # so undo in gout does not bring the preview's settings back
                    project.conn.execute("UPDATE history SET snapshot = ? WHERE id = ?", (json.dumps(snap), row["id"]))
            project.sync_json()
        finally:
            project.conn.close()
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for name in (DB_NAME, "gout.json"):
                    z.write(work / name, f"{ZIP_FOLDER}/{name}")
                z.writestr(f"{ZIP_FOLDER}/{TRACK_DIR}/", "")
                for f in sorted((root / TRACK_DIR).iterdir()):
                    if f.is_file() and ".part" not in f.name:
                        z.write(f, f"{ZIP_FOLDER}/{TRACK_DIR}/{f.name}", compress_type=zipfile.ZIP_STORED)
                z.writestr(f"{ZIP_FOLDER}/README.txt", ZIP_README)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return out


CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}
PAGE_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
               "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
               "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = f"gout/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if not getattr(self.server, "quiet", False):
            sys.stderr.write(f"{self.address()} {format % args}\n")

    def address(self) -> str:
        if getattr(self.server, "trust_proxy", False):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded.strip():
                return forwarded.split(",")[-1].strip()  # what the proxy in front saw
        return self.client_address[0]

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PUT(self) -> None:
        self.route("PUT")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    # ---- replies

    def send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def send_file(self, path: Path, content_type: str, headers: dict | None = None) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 16)

    def json_body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size > JSON_LIMIT:
            self.close_connection = True
            raise Refused(413, "error: request too large")
        raw = self.rfile.read(size) if size else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise Refused(400, "error: the request is not json")
        if not isinstance(body, dict):
            raise Refused(400, "error: the request is not a json object")
        return body

    # ---- routes

    def route(self, method: str) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path.startswith("/api/"):
                self.api(method, path[len("/api/"):], query)
            elif method == "GET":
                self.static(path)
            else:
                raise Refused(405, "error: method not allowed")
        except Refused as exc:
            if method == "PUT":
                self.close_connection = True  # the file may be unread: the connection cannot be reused
            self.send_json(exc.status, {"ok": False, "error": exc.message, "lines": exc.message.splitlines(),
                                        **exc.extra})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            try:
                self.send_json(500, {"ok": False, "error": "error: the server hit a problem",
                                     "lines": ["error: the server hit a problem"]})
            except OSError:
                pass

    def static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self.send(200, (PAGE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8",
                      {"Content-Security-Policy": PAGE_POLICY})
            return
        if path == "/theme.css":
            self.send(200, theme_css().encode(), "text/css; charset=utf-8")
            return
        name = path.lstrip("/")
        if name.startswith("downloads/") and self.app.downloads:
            file = name[len("downloads/"):]
            target = self.app.downloads / file
            if re.fullmatch(r"[A-Za-z0-9._-]+", file) and target.is_file():
                self.send_file(target, "application/octet-stream",
                               {"Content-Disposition": f'attachment; filename="{file}"'})
                return
        if re.fullmatch(r"[a-z0-9_-]+\.(html|js|css|svg|png|ico|txt)", name) and (PAGE_DIR / name).is_file():
            self.send(200, (PAGE_DIR / name).read_bytes(), CONTENT_TYPES[Path(name).suffix])
            return
        raise Refused(404, "not found")

    def project_root(self) -> Path:
        root = self.app.store.folder(self.headers.get(KEY_HEADER))
        if root is None:
            raise Refused(401, "this browser's project is gone (unused for a week, or deleted)", gone=True)
        return root

    def api(self, method: str, name: str, query: dict) -> None:
        app, store = self.app, self.app.store
        if (method, name) == ("GET", "info"):
            self.send_json(200, app.info())
            return
        if (method, name) == ("GET", "cheat"):
            self.send_json(200, cheat_payload(int((query.get("width") or ["100"])[0] or 100)))
            return
        if (method, name) == ("GET", "formula"):
            text = (query.get("text") or [""])[0]
            try:
                formula = parse(text)
            except FormulaError as exc:
                raise Refused(400, f"fractal: {exc}")
            self.send_json(200, {"text": formula.text, "pretty": formula.pretty, "program": formula.program()})
            return
        if (method, name) == ("POST", "project"):
            self.json_body()
            key = store.create(self.address())
            self.send_json(201, {"key": key, **app.info()})
            return

        root = self.project_root()
        if (method, name) == ("PUT", "files"):
            size = self.headers.get("Content-Length")
            if size is None or not size.isdigit():
                self.close_connection = True
                raise Refused(411, "upload: the size of the file is missing")
            filename = (query.get("name") or [""])[0]
            with store.lock(root):
                store.touch(root)
                payload = app.upload(root, filename, int(size), self.rfile)
                payload |= app.state(root, {"width": (query.get("width") or ["100"])[0]})
            self.send_json(201, payload)
            return
        with store.lock(root):
            if (method, name) == ("DELETE", "project"):
                store.delete(root)
                self.send_json(200, {"ok": True, "lines": ["project deleted"]})
                return
            store.touch(root)
            if (method, name) == ("POST", "state"):
                self.send_json(200, app.state(root, self.json_body()))
            elif (method, name) == ("POST", "run"):
                self.send_json(200, app.run(root, self.json_body()))
            elif method == "DELETE" and name.startswith("files/"):
                body = self.json_body()
                payload = app.remove(root, name[len("files/"):])
                self.send_json(200, payload | app.state(root, body))
            elif (method, name) == ("GET", "mix.mp3"):
                path, head = app.mix(root)
                headers = {"X-Gout-Head-Ms": str(head), "X-Gout-Mix-State": app.mix_state(root)}
                if (query.get("download") or [""])[0]:
                    headers["Content-Disposition"] = f'attachment; filename="{ZIP_FOLDER}.mp3"'
                self.send_file(path, "audio/mpeg", headers)
            elif (method, name) == ("GET", "analysis"):
                self.send_json(200, app.features(root))
            elif (method, name) == ("GET", "project.zip"):
                out = app.zip_project(root)
                try:
                    self.send_file(out, "application/zip",
                                   {"Content-Disposition": f'attachment; filename="{ZIP_FOLDER}.zip"'})
                finally:
                    out.unlink(missing_ok=True)
            else:
                raise Refused(404, "not found")


def default_root() -> Path:
    env = os.environ.get("GOUT_WEB_ROOT")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "gout" / "web"


def serve(host: str, port: int, root: Path, downloads: Path | None = None, trust_proxy: bool = False,
          quiet: bool = False, releases: str | None = None) -> None:
    effects()  # built-ins only: cli.run switched addons off before anything loaded
    store = Store(root)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = App(store, downloads, Releases(releases))  # type: ignore[attr-defined]
    server.trust_proxy = trust_proxy  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]

    def hourly() -> None:
        while True:
            server.app.releases.refresh()  # type: ignore[attr-defined]
            for name in store.sweep():
                print(f"web   deleted unused project {name[:8]}…", flush=True)
            time.sleep(3600)

    threading.Thread(target=hourly, daemon=True).start()
    shown = host if ":" not in host else f"[{host}]"
    print(f"web   http://{shown}:{server.server_address[1]}/  projects in {store.root}  (ctrl-c stops)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
