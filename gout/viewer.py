"""The window: a high-definition timeline in a browser, next to the ui (ctrl-o, or window).

The ui starts a Viewer, a small web server on 127.0.0.1 at a port the system picks. Every request
for data carries a secret token that only the link gout opens knows, and a request naming any other
host is turned away, so no other program or web page can read the project. The page draws the
tracks with real waveforms, zooms down to single samples and moves its playhead on its own between
the ui's reports, so it glides.

The ui thread hands the server what the page needs: publish_state when the project changed, and
publish_play every turn of its loop (where the playhead is, whether it plays, a take as it grows).
The server's threads read only those and the audio files, never the database: sqlite connections
stay on the thread that made them. Keys and clicks from the page come back through a queue the ui
empties each turn, so everything that changes something still happens in the ui.

Waveforms: ffmpeg decodes a file once to 32-bit float at the project rate; the lowest and highest
sample of every PEAK_BLOCK frames (both channels together) make the finest level, and each coarser
level takes four points of the one below. Measured: 5 minutes take about 1.7 s, and 5 minutes of the
finest level are about 0.5 MB. The levels are cached in .gout/peaks/ by file size, time and rate.
Closer in than the finest level, the page asks for the samples themselves (a 50 ms piece: 91 ms).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import queue
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import detached, frozen, pan_filter, resource_dir

PEAK_BLOCK = 256       # frames per point at the finest level
PEAK_LEVELS = 5        # the finest and four coarser ones, each four times coarser
EVENTS_EVERY = 0.05    # seconds between reports to the page
MAX_SAMPLES_S = 4.0    # the longest piece of a file sent sample by sample
PAGE_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
              "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
              "/viewer.css": ("viewer.css", "text/css; charset=utf-8")}
APP_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser",
                "microsoft-edge")


def page_dir() -> Path:
    return resource_dir() / "gout" / "viewerpage" if frozen() else Path(__file__).resolve().parent / "viewerpage"


def channels_of(path: Path) -> int:
    from .media import probe
    try:
        return probe(path)["channels"] or 2
    except Exception:  # noqa: BLE001 - a file ffprobe cannot read draws as nothing
        return 2


def decode(path: Path, rate: int, start_s: float = 0.0, seconds: float | None = None, channels: int = 2) -> array:
    """A file (or a piece of it) as interleaved stereo 32-bit float at rate, made stereo the way the
    mix does it: mono at full level on both sides (ffmpeg's own upmix would draw it 3 dB smaller)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.6f}"]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.6f}"]
    cmd += ["-i", str(path)]
    if channels != 2:
        cmd += ["-af", pan_filter(channels, 0.0)]
    cmd += ["-f", "f32le", "-ac", "2", "-ar", str(rate), "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    data = array("f")
    data.frombytes(out[:len(out) // 4 * 4])
    return data


def peak_levels(path: Path, rate: int, cache_dir: Path | None) -> list[array]:
    """The lowest and highest sample per PEAK_BLOCK frames, then per 4, 16 ... times that, as
    [lo0, hi0, lo1, hi1, ...] arrays per level."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = hashlib.sha1(f"{path.name}|{st.st_size}|{st.st_mtime}|{rate}|{PEAK_BLOCK}|{PEAK_LEVELS}|2".encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}.peaks" if cache_dir is not None else None
    if cached is not None and cached.exists():
        blob = cached.read_bytes()
        counts = array("I")
        counts.frombytes(blob[:4 * PEAK_LEVELS])
        levels, at = [], 4 * PEAK_LEVELS
        for count in counts:
            level = array("f")
            level.frombytes(blob[at:at + 4 * count])
            levels.append(level)
            at += 4 * count
        return levels
    samples = decode(path, rate, channels=channels_of(path))
    span = PEAK_BLOCK * 2
    finest = array("f")
    for i in range(0, len(samples), span):
        piece = samples[i:i + span]
        finest.append(min(piece))
        finest.append(max(piece))
    levels = [finest]
    for _ in range(PEAK_LEVELS - 1):
        below, level = levels[-1], array("f")
        for i in range(0, len(below), 8):
            level.append(min(below[i:i + 8:2]))
            level.append(max(below[i + 1:i + 8:2]))
        levels.append(level)
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        with open(tmp, "wb") as f:
            f.write(array("I", [len(level) for level in levels]).tobytes())
            for level in levels:
                f.write(level.tobytes())
        tmp.replace(cached)
    return levels


def app_browser() -> str | None:
    return next((found for name in APP_BROWSERS if (found := shutil.which(name))), None)


class Viewer:
    """The window's server. start() and stop() from the ui; publish_* from the ui thread only."""

    def __init__(self, root: Path, rate: int):
        self.root, self.rate = root, rate
        self.token = secrets.token_urlsafe(18)
        self.state = b"{}"
        self.version = 0
        self.files: dict[str, Path] = {}      # what the page may ask about, by the name it knows
        self.play: dict = {"pos": 0, "playing": False, "take": None}
        self.keys: queue.Queue = queue.Queue()
        self.levels: dict[str, list[array]] = {}
        self.levels_lock = threading.Lock()
        self.known_channels: dict[Path, int] = {}
        self.stopping = False
        viewer = self

        class Handler(RequestHandler):
            owner = viewer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> "Viewer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping = True
        self.server.shutdown()
        self.server.server_close()

    def open(self) -> str:
        """Show the page: an app window of its own when a Chromium-like browser is there, else a tab."""
        browser = app_browser()
        if browser:
            try:
                subprocess.Popen([browser, f"--app={self.url}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **detached())
                return Path(browser).name
            except OSError:
                pass
        webbrowser.open(self.url)
        return "the default browser"

    def publish_state(self, state: dict, files: dict[str, Path]) -> None:
        self.files = dict(files)
        self.version += 1
        self.state = json.dumps({**state, "version": self.version}).encode()

    def publish_play(self, pos_ms: int, playing: bool, take: dict | None) -> None:
        self.play = {"pos": pos_ms, "playing": playing, "take": take}

    def channels(self, path: Path) -> int:
        if path not in self.known_channels:
            self.known_channels[path] = channels_of(path)
        return self.known_channels[path]

    def peaks(self, name: str) -> list[array]:
        path = self.files.get(name)
        if path is None:
            return []
        stamp = f"{name}|{path.stat().st_mtime if path.exists() else 0}"
        with self.levels_lock:  # two requests for one file work it out once
            if stamp not in self.levels:
                self.levels[stamp] = peak_levels(path, self.rate, self.root / ".gout" / "peaks")
            return self.levels[stamp]


class RequestHandler(BaseHTTPRequestHandler):
    owner: Viewer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # the ui owns the terminal
        pass

    def allowed(self, needs_token: bool) -> bool:
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.owner.port}", f"localhost:{self.owner.port}"):
            self.send_error(403, "wrong host")
            return False
        if needs_token:
            given = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            if not hmac.compare_digest(given, self.owner.token):
                self.send_error(403, "no token")
                return False
        return True

    def reply(self, body: bytes, kind: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in PAGE_FILES:
            if not self.allowed(needs_token=False):
                return
            name, kind = PAGE_FILES[url.path]
            self.reply((page_dir() / name).read_bytes(), kind)
            return
        if not self.allowed(needs_token=True):
            return
        if url.path == "/state":
            self.reply(self.owner.state, "application/json")
        elif url.path == "/peaks":
            levels = self.owner.peaks(query.get("file", [""])[0])
            if not levels:
                self.send_error(404, "no such file")
                return
            head = array("I", [self.owner.rate, PEAK_BLOCK, len(levels), *[len(level) // 2 for level in levels]])
            self.reply(head.tobytes() + b"".join(level.tobytes() for level in levels), "application/octet-stream")
        elif url.path == "/samples":
            path = self.owner.files.get(query.get("file", [""])[0])
            try:
                start, end = float(query.get("from", ["0"])[0]), float(query.get("to", ["0"])[0])
            except ValueError:
                start = end = 0.0
            if path is None or end <= start:
                self.send_error(404, "no such piece")
                return
            start = max(0.0, start)
            data = decode(path, self.owner.rate, start / 1000, min(MAX_SAMPLES_S, (end - start) / 1000),
                          self.owner.channels(path))
            self.reply(array("f", [start, float(self.owner.rate)]).tobytes() + data.tobytes(), "application/octet-stream")
        elif url.path == "/events":
            self.events()
        else:
            self.send_error(404)

    def events(self) -> None:
        """Server-sent events: where the playhead is, the state's version, a take's new peaks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent_peaks, take_at = 0, None
        try:
            while not self.owner.stopping:
                play = self.owner.play
                message = {"v": self.owner.version, "pos": play["pos"], "playing": play["playing"], "take": None}
                take = play["take"]
                if take is not None:
                    if take["at"] != take_at:
                        take_at, sent_peaks = take["at"], 0
                    peaks = take["peaks"]
                    message["take"] = {"at": take["at"], "label": take["label"], "from": sent_peaks,
                                       "peaks": [round(p, 4) for p in peaks[sent_peaks:]]}
                    sent_peaks = len(peaks)
                else:
                    take_at = None
                self.wfile.write(f"data: {json.dumps(message)}\n\n".encode())
                self.wfile.flush()
                time.sleep(EVENTS_EVERY)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:
        if not self.allowed(needs_token=True):
            return
        size = min(4096, int(self.headers.get("Content-Length") or 0))
        try:
            message = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            message = {}
        if urlparse(self.path).path == "/key" and isinstance(message, dict):
            self.owner.keys.put(message)
            self.reply(b"", "text/plain", 204)
        else:
            self.send_error(404)


def project_state(project, theme: dict) -> tuple[dict, dict[str, Path]]:
    """What the page draws, from the ui's thread (it reads the database): the tracks with their parts,
    the master, colours from color.json. And the files the page may ask for, by name."""
    from .model import audible, is_heard, part_label, timeline
    from .settings import setting
    from .video import rgb

    def hex_of(value) -> str:
        return "#%02x%02x%02x" % rgb(value)

    tracks = project.tracks()
    any_solo = any(t["solo"] for t in tracks)
    files: dict[str, Path] = {}

    def stamp(path: Path) -> str:
        try:
            st = path.stat()
            return f"{st.st_size}-{st.st_mtime}"
        except OSError:
            return ""

    out = []
    for i, t in enumerate(tracks):
        a, b = audible(t)
        path = project.tracks_dir / t["file"]
        files[t["file"]] = path
        out.append({"n": t["n"], "name": t["name"], "file": t["file"], "stamp": stamp(path),
                    "offset_ms": t["offset_ms"], "in_ms": a, "out_ms": b, "length_ms": t["length_ms"],
                    "gain_db": t["gain_db"], "pan": t["pan"], "mute": bool(t["mute"]), "solo": bool(t["solo"]),
                    "heard": is_heard(t, any_solo), "color": i, "fx": [item["kind"] for item in t["fx"]],
                    "parts": [{"label": part_label(t["parts"], p), "in_ms": p["in_ms"], "out_ms": p["out_ms"],
                               "shift_ms": p["shift_ms"], "mute": bool(p["mute"]), "gain_db": p["gain_db"],
                               "fx": [item["kind"] for item in p.get("fx", [])]} for p in t["parts"]]})
    master = None
    master_ms = int(project.get("master_ms") or 0)
    if project.master.exists() and master_ms:
        files["master.wav"] = project.master
        master = {"file": "master.wav", "stamp": stamp(project.master), "length_ms": master_ms,
                  "head_ms": int(setting(project, "head")), "current": project.master_is_current()}
    ends = [timeline(t)[1] for t in tracks] + ([master_ms - master["head_ms"]] if master else [])
    bpm = setting(project, "bpm")
    colours = {name: hex_of(theme[name]) for name in ("master_wave", "track_label", "muted_wave", "trimmed_wave",
                                                     "center_line", "ruler", "ruler_labels", "playhead", "gap_line")}
    colours["track_palette"] = [hex_of(c) for c in theme["track_palette"]]
    state = {"name": project.get("name"), "rate": project.rate, "bpm": float(bpm) if bpm else None,
             "length_ms": max(ends, default=0), "master": master, "tracks": out, "colours": colours}
    return state, files
