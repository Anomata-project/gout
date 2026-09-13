#!/usr/bin/env python3
"""gout — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

    gout new song && cd song
    gout add drums.mp3 bass.wav        # copied into master/, master.wav is mixed
    gout move 2 +1.5s                  # nudge the bass 1.5 s later, mix again
    gout cut show.mp3 -st 34 -fs 1.99  # the 1.x cutter, unchanged
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

__version__ = "2.0.0"

DB_NAME = "gout.db"
TRACK_DIR = "master"
MASTER_WAV = "master.wav"
MASTER_MP3 = "master.mp3"
DEFAULT_RATE = 48000

# How close to the size limit we insist on landing before we stop refining.
SIZE_ACCEPT = 0.97
SIZE_MAX_PASSES = 6
# Bytes to hold back on the very first guess for the ID3 tag / Xing header.
TAG_ALLOWANCE = 4096

UNITS = {
    "": 1000**2,  # bare number means megabytes
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ki": 1024,
    "kib": 1024,
    "mi": 1024**2,
    "mib": 1024**2,
    "gi": 1024**3,
    "gib": 1024**3,
    "ti": 1024**4,
    "tib": 1024**4,
}


class GoutError(Exception):
    """A user-facing failure. main() prints it and exits 1; the UI shows it."""


def die(msg: str) -> "NoReturn":  # noqa: F821
    raise GoutError(msg)


def need_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} not found on PATH — install ffmpeg first")


# --------------------------------------------------------------------------- parsing


def parse_time(text: str) -> float:
    """Parse a time/duration into seconds.

    HH:MM:SS.mmm   HH:MM:SS:mmm   MM:SS   90s   2.5m   1.5h   34 (bare = minutes)
    """
    s = text.strip().lower()
    if not s:
        raise ValueError("empty time")

    if ":" in s:
        parts = s.split(":")
        if len(parts) == 4:  # HH:MM:SS:MS -- last field is milliseconds
            h, m, sec, ms = parts
            parts = [h, m, f"{float(sec) + int(ms) / 1000:.6f}"]
        if len(parts) == 2:
            parts = ["0", *parts]
        if len(parts) != 3:
            raise ValueError(f"bad time {text!r}")
        h, m, sec = (float(p) if p.strip() else 0.0 for p in parts)
        total = h * 3600 + m * 60 + sec
    else:
        match = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)\s*(ms|s|sec|m|min|h|hr)?", s)
        if not match:
            raise ValueError(f"bad time {text!r}")
        value = float(match.group(1))
        total = value * {
            None: 60,  # bare number means minutes
            "ms": 0.001,
            "s": 1,
            "sec": 1,
            "m": 60,
            "min": 60,
            "h": 3600,
            "hr": 3600,
        }[match.group(2)]

    if total < 0:
        raise ValueError("time cannot be negative")
    return total


def parse_ms(text: str) -> int:
    try:
        return round(parse_time(text) * 1000)
    except ValueError as exc:
        die(str(exc))


def parse_size(text: str) -> int:
    """Parse a file size into bytes. Bare numbers are megabytes.

    MB/GB are decimal (1 MB = 1000000 B); use MiB/GiB for the binary units.
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text.strip().lower())
    if not match:
        raise ValueError(f"bad size {text!r}")
    unit = match.group(2)
    if unit not in UNITS:
        raise ValueError(f"unknown size unit {unit!r} in {text!r}")
    size = int(float(match.group(1)) * UNITS[unit])
    if size <= 0:
        raise ValueError("size must be positive")
    return size


def fmt_time(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    ms = round(abs(seconds) * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_ms(ms: int) -> str:
    return fmt_time(ms / 1000)


def fmt_short(ms: int, decimals: int = 0) -> str:
    """Compact m:ss(.d) for the timeline axis; h:mm:ss once past an hour."""
    sign = "-" if ms < 0 else ""
    total = abs(ms) / 1000
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    sec = f"{s:0{3 + decimals}.{decimals}f}" if decimals else f"{int(s):02d}"
    if h >= 1:
        return f"{sign}{int(h)}:{int(m):02d}:{sec}"
    return f"{sign}{int(m)}:{sec}"


def fmt_size(nbytes: int) -> str:
    for unit, scale in (("GB", 1000**3), ("MB", 1000**2), ("kB", 1000)):
        if nbytes >= scale:
            return f"{nbytes / scale:.2f} {unit}"
    return f"{nbytes} B"


def fmt_db(gain: float) -> str:
    return f"{gain:+.1f}dB"


def fmt_pan(pan: float) -> str:
    if abs(pan) < 0.005:
        return "C"
    return f"{'L' if pan < 0 else 'R'}{round(abs(pan) * 100)}"


# --------------------------------------------------------------------------- ffmpeg


def run_quiet(cmd: list[str], verbose: bool = False) -> subprocess.CompletedProcess:
    if verbose:
        print("  $ " + " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"{cmd[0]} failed:\n{result.stderr.strip()}")
    return result


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,bit_rate,channels,sample_rate:format=duration,bit_rate,format_name",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        die(f"no audio stream found in {path}")
    fmt = data.get("format") or {}

    duration = float(fmt.get("duration") or 0.0)
    if duration <= 0:
        die(f"could not determine the duration of {path}")

    bitrate = 0.0
    for candidate in (streams[0].get("bit_rate"), fmt.get("bit_rate")):
        try:
            bitrate = float(candidate)
        except (TypeError, ValueError):
            continue
        if bitrate > 0:
            break
    if bitrate <= 0:  # last resort: average over the whole file
        bitrate = path.stat().st_size * 8 / duration

    return {
        "duration": duration,
        "bitrate": bitrate,
        "codec": streams[0].get("codec_name") or "?",
        "channels": int(streams[0].get("channels") or 0),
        "sample_rate": int(streams[0].get("sample_rate") or 0),
        "format": fmt.get("format_name") or "",
    }


def cut(src: Path, dst: Path, start: float, duration: float | None,
        reencode: bool, bitrate: float, verbose: bool, codec: str = "mp3") -> None:
    """Write [start, start+duration) of src to dst.

    mp3 is stream-copied (frame-accurate) unless reencode; wav is rewritten with
    its own pcm codec, which is lossless and sample-accurate.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-map_metadata", "0"]
    if codec.startswith("pcm_"):
        cmd += ["-c:a", codec]
    else:
        cmd += ["-id3v2_version", "3", "-write_xing", "1"]
        if reencode:
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{max(32, round(bitrate / 1000))}k"]
        else:
            cmd += ["-c:a", "copy"]
    cmd.append(str(dst))
    run_quiet(cmd, verbose)


def cut_to_size(src: Path, dst: Path, start: float, limit: int, remaining: float,
                reencode: bool, bitrate: float, verbose: bool) -> tuple[float, int]:
    """Cut the longest piece from `start` that still fits in `limit` bytes."""
    guess = max(0.05, (limit - TAG_ALLOWANCE) * 8 / bitrate)
    duration = min(guess, remaining)
    best: tuple[float, int] | None = None
    last = duration

    for attempt in range(1, SIZE_MAX_PASSES + 1):
        cut(src, dst, start, duration, reencode, bitrate, verbose)
        size = dst.stat().st_size
        last = duration
        if verbose:
            print(f"  pass {attempt}: {fmt_time(duration)} -> {fmt_size(size)}"
                  f" ({size / limit:.1%} of limit)", file=sys.stderr)

        if size <= limit and (best is None or duration > best[0]):
            best = (duration, size)
        if limit * SIZE_ACCEPT <= size <= limit:
            break
        if size <= limit and duration >= remaining - 1e-3:
            break  # already taking everything that is left

        scaled = min(duration * (limit * 0.998) / size, remaining)
        if abs(scaled - duration) < 0.05:
            break
        duration = max(0.05, scaled)

    if best is None:
        die(f"cannot fit anything into {fmt_size(limit)} — try a bigger --file-size")
    if abs(best[0] - last) > 1e-6:  # the last pass was not the keeper, redo it
        cut(src, dst, start, best[0], reencode, bitrate, verbose)
        best = (best[0], dst.stat().st_size)
    return best


# --------------------------------------------------------------------------- project store

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    n           INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    file        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    length_ms   INTEGER NOT NULL,
    channels    INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    offset_ms   INTEGER NOT NULL DEFAULT 0,
    in_ms       INTEGER NOT NULL DEFAULT 0,
    out_ms      INTEGER,
    gain_db     REAL NOT NULL DEFAULT 0,
    pan         REAL NOT NULL DEFAULT 0,
    mute        INTEGER NOT NULL DEFAULT 0,
    solo        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    command  TEXT NOT NULL,
    undoable INTEGER NOT NULL DEFAULT 1,
    snapshot TEXT NOT NULL
);
"""

TRACK_COLUMNS = ("n", "name", "file", "kind", "length_ms", "channels", "sample_rate",
                 "offset_ms", "in_ms", "out_ms", "gain_db", "pan", "mute", "solo")


class Project:
    """A project directory: gout.db, master/ with the track files, master.wav."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.db_path = self.root / DB_NAME
        self.tracks_dir = self.root / TRACK_DIR
        self.master = self.root / MASTER_WAV
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- locating

    @classmethod
    def find(cls, start: Path | None = None) -> "Project | None":
        here = (start or Path.cwd()).resolve()
        for candidate in (here, *here.parents):
            if (candidate / DB_NAME).is_file():
                return cls(candidate)
        return None

    @classmethod
    def create(cls, root: Path, rate: int) -> "Project":
        root.mkdir(parents=True, exist_ok=True)
        (root / TRACK_DIR).mkdir(exist_ok=True)
        project = cls(root)
        project.set("name", root.resolve().name)
        project.set("rate", str(rate))
        project.set("automix", "on")
        project.set("created", dt.datetime.now().isoformat(timespec="seconds"))
        return project

    # ---- settings

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM project WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO project (key, value) VALUES (?, ?)", (key, value))

    def unset(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM project WHERE key = ?", (key,))

    @property
    def rate(self) -> int:
        return int(self.get("rate") or DEFAULT_RATE)

    @property
    def automix(self) -> bool:
        return (self.get("automix") or "on") != "off"

    # ---- tracks

    def tracks(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM tracks ORDER BY n").fetchall()
        return [dict(r) for r in rows]

    def track(self, spec: str) -> dict:
        tracks = self.tracks()
        if not tracks:
            die("the project has no tracks yet — gout add FILE")
        if spec.isdigit():
            for t in tracks:
                if t["n"] == int(spec):
                    return t
            die(f"no track {spec} (there are {len(tracks)})")
        exact = [t for t in tracks if t["name"] == spec]
        if exact:
            return exact[0]
        prefix = [t for t in tracks if t["name"].startswith(spec)]
        if len(prefix) == 1:
            return prefix[0]
        if prefix:
            die(f"'{spec}' matches several tracks: " + ", ".join(t["name"] for t in prefix))
        die(f"no track named '{spec}'")

    def update(self, n: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE tracks SET {cols} WHERE n = ?", (*fields.values(), n))

    def insert(self, **fields) -> dict:
        fields.setdefault("n", (self.conn.execute("SELECT MAX(n) FROM tracks").fetchone()[0] or 0) + 1)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn:
            self.conn.execute(f"INSERT INTO tracks ({cols}) VALUES ({marks})", tuple(fields.values()))
        return self.track(str(fields["n"]))

    def delete(self, n: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tracks WHERE n = ?", (n,))
            rows = self.conn.execute("SELECT n FROM tracks ORDER BY n").fetchall()
            for new, row in enumerate(rows, 1):  # keep numbering contiguous
                if row["n"] != new:
                    self.conn.execute("UPDATE tracks SET n = ? WHERE n = ?", (new, row["n"]))

    def unique_name(self, wanted: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", wanted).strip("-.") or "track"
        taken = {t["name"] for t in self.tracks()}
        taken |= {p.stem for p in self.tracks_dir.iterdir()} if self.tracks_dir.is_dir() else set()
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        return name

    # ---- history

    def snapshot(self) -> dict:
        settings = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM project")}
        return {"project": settings, "tracks": self.tracks()}

    def record(self, command: str, undoable: bool = True) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO history (ts, command, undoable, snapshot) VALUES (?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), command, int(undoable),
                 json.dumps(self.snapshot())),
            )

    def restore(self, snap: dict) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tracks")
            for t in snap["tracks"]:
                cols = [c for c in TRACK_COLUMNS if c in t]
                self.conn.execute(
                    f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple(t[c] for c in cols),
                )
            self.conn.execute("DELETE FROM project")
            for k, v in snap["project"].items():
                self.conn.execute("INSERT INTO project (key, value) VALUES (?, ?)", (k, v))

    def undo(self) -> str:
        row = self.conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            die("nothing to undo")
        if not row["undoable"]:
            die(f"cannot undo '{row['command']}': it rewrote or deleted an audio file")
        snap = json.loads(row["snapshot"])
        before = {t["file"] for t in self.tracks()}
        after = {t["file"] for t in snap["tracks"]}
        self.restore(snap)
        for stray in before - after:  # a file we copied in and are now backing out
            with_path = self.tracks_dir / stray
            if with_path.exists():
                with_path.unlink()
        with self.conn:
            self.conn.execute("DELETE FROM history WHERE id = ?", (row["id"],))
        return row["command"]


# --------------------------------------------------------------------------- timeline maths


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


# --------------------------------------------------------------------------- mixing


def pan_filter(channels: int, pan: float) -> str:
    """Stereo output, centre by default: mono goes equally to L and R."""
    left = min(1.0, 1.0 - pan)
    right = min(1.0, 1.0 + pan)
    if channels == 1:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0"
    if channels == 2:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"
    return f"aformat=channel_layouts=stereo,pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"


def build_graph(project: Project, tracks: list[dict]) -> tuple[list[Path], str, list[dict]]:
    any_solo = any(t["solo"] for t in tracks)
    inputs: list[Path] = []
    chains: list[str] = []
    used: list[dict] = []
    for t in tracks:
        if not is_heard(t, any_solo):
            continue
        a, b = audible(t)
        start = t["offset_ms"] + a
        trim_start, delay = a, start
        if start < 0:  # the head hangs before the timeline: cut it, no delay
            trim_start, delay = a - start, 0
        if b <= trim_start:
            continue
        i = len(inputs)
        inputs.append(project.tracks_dir / t["file"])
        used.append(t)
        steps = [f"aformat=sample_rates={project.rate}:sample_fmts=fltp"]
        if trim_start > 0 or b < t["length_ms"]:
            steps.append(f"atrim=start={trim_start / 1000:.3f}:end={b / 1000:.3f}")
            steps.append("asetpts=PTS-STARTPTS")
        if delay > 0:
            steps.append(f"adelay={delay}:all=1")
        if t["gain_db"]:
            steps.append(f"volume={t['gain_db']:.2f}dB")
        steps.append(pan_filter(t["channels"], t["pan"]))
        chains.append(f"[{i}:a]" + ",".join(steps) + f"[t{i}]")
    if not inputs:
        return [], "", []
    labels = "".join(f"[t{i}]" for i in range(len(inputs)))
    chains.append(f"{labels}amix=inputs={len(inputs)}:normalize=0:duration=longest"
                  f":dropout_transition=0[mix]")
    return inputs, ";".join(chains), used


def peak_dbfs(path: Path) -> float | None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "astats=measure_overall=Peak_level:measure_perchannel=none", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"Peak level dB:\s*(-?[\d.]+|-inf)", result.stderr)
    if not match:
        return None
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


def mix(project: Project, verbose: bool = False, mp3: bool = False) -> None:
    tracks = project.tracks()
    inputs, graph, used = build_graph(project, tracks)
    if not inputs:
        if project.master.exists():
            project.master.unlink()
        for key in ("master_ms", "master_peak"):
            project.unset(key)
        print("mix   nothing audible" + (" — master.wav removed" if tracks else "")
              + ("" if tracks else " (no tracks yet)"))
        return

    tmp = project.root / (MASTER_WAV + ".part.wav")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", "[mix]", "-ar", str(project.rate),
            "-c:a", "pcm_f32le", str(tmp)]
    try:
        run_quiet(cmd, verbose)
        tmp.replace(project.master)
    finally:
        if tmp.exists():
            tmp.unlink()

    length_ms = round(probe(project.master)["duration"] * 1000)
    peak = peak_dbfs(project.master)
    project.set("master_ms", str(length_ms))
    project.set("master_peak", "" if peak is None else f"{peak:.2f}")
    skipped = len(tracks) - len(used)
    note = f"  ({len(used)} of {len(tracks)} tracks)" if skipped else ""
    peak_txt = "" if peak is None else f"  peak {peak:+.1f} dBFS"
    print(f"mix   {MASTER_WAV}  {fmt_ms(length_ms)}{peak_txt}{note}")
    if peak is not None and peak > 0:
        print("      peak is above 0 dBFS: the float wav is fine, but lower some gain before"
              " exporting or playing it back loud")
    if mp3:
        out = project.root / MASTER_MP3
        run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(project.master),
                   "-c:a", "libmp3lame", "-b:a", "320k", str(out)], verbose)
        print(f"      {MASTER_MP3}  {fmt_size(out.stat().st_size)}")


def automix(project: Project, args: "Args") -> None:
    if args.no_mix:
        return
    if project.automix:
        mix(project)


# --------------------------------------------------------------------------- command args


class Args:
    """Tiny flag parser. Values like -2s or +1.5s stay positional; unknown -x flags fail."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        self.no_mix = self.flag("--no-mix")
        self.verbose = self.flag("-v", "--verbose")

    def flag(self, *names: str) -> bool:
        hit = False
        for name in names:
            while name in self.argv:
                self.argv.remove(name)
                hit = True
        return hit

    def value(self, *names: str, default: str | None = None) -> str | None:
        for name in names:
            for i, a in enumerate(self.argv):
                if a == name:
                    if i + 1 >= len(self.argv):
                        die(f"{name} needs a value")
                    val = self.argv[i + 1]
                    del self.argv[i:i + 2]
                    return val
                if a.startswith(name + "="):
                    del self.argv[i]
                    return a[len(name) + 1:]
        return default

    def positionals(self, usage: str, lo: int = 0, hi: int | None = None) -> list[str]:
        for a in self.argv:
            if len(a) > 1 and a[0] == "-" and not (a[1].isdigit() or a[1] in ".+="):
                die(f"unknown flag {a}\nusage: {usage}")
        if len(self.argv) < lo or (hi is not None and len(self.argv) > hi):
            die(f"usage: {usage}")
        return self.argv


def on_off(text: str | None, current: int) -> int:
    if text is None:
        return 0 if current else 1
    if text.lower() in ("on", "1", "yes", "true"):
        return 1
    if text.lower() in ("off", "0", "no", "false"):
        return 0
    die(f"expected on or off, got {text!r}")


# --------------------------------------------------------------------------- project commands


def cmd_new(root_hint: Path | None, args: Args) -> None:
    rate_txt = args.value("--rate", default=str(DEFAULT_RATE))
    (name,) = args.positionals("gout new NAME [--rate HZ]", 1, 1)
    if not rate_txt.isdigit() or not 8000 <= int(rate_txt) <= 384000:
        die(f"bad sample rate {rate_txt!r}")
    root = Path.cwd() if name == "." else Path(name)
    if (root / DB_NAME).exists():
        die(f"{root / DB_NAME} already exists")
    project = Project.create(root, int(rate_txt))
    print(f"new   {project.root}  ({project.rate} Hz, tracks go in {TRACK_DIR}/)")


def ingest(project: Project, src: Path, name: str | None, at_ms: int, verbose: bool) -> dict:
    if not src.is_file():
        die(f"no such file: {src}")
    info = probe(src)
    suffix = src.suffix.lower()
    if info["codec"] == "mp3" and suffix == ".mp3":
        kind, ext = "mp3", ".mp3"
    elif info["codec"].startswith("pcm_") and suffix == ".wav":
        kind, ext = "wav", ".wav"
    else:
        kind, ext = "wav", ".wav"  # anything else is decoded to wav on the way in

    in_place = src.resolve().parent == project.tracks_dir.resolve() and ext == suffix
    if in_place and name is None and not any(t["file"] == src.name for t in project.tracks()):
        track_name, dst = src.stem, src
    else:
        track_name = project.unique_name(name or src.stem)
        dst = project.tracks_dir / f"{track_name}{ext}"
        if kind == "mp3" or (kind == "wav" and suffix == ".wav" and info["codec"].startswith("pcm_")):
            shutil.copy2(src, dst)
        else:
            run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                       "-map", "0:a:0", "-c:a", "pcm_s24le", str(dst)], verbose)
            info = probe(dst)

    track = project.insert(
        name=track_name, file=dst.name, kind=kind,
        length_ms=round(info["duration"] * 1000), channels=info["channels"] or 2,
        sample_rate=info["sample_rate"] or project.rate, offset_ms=at_ms,
    )
    how = "kept in" if dst == src else ("copied to" if ext == suffix else "converted to")
    print(f"add   {track['n']:>2}  {track['name']:<16} {kind}  {info['channels']}ch  {info['sample_rate']} Hz"
          f"  {fmt_ms(track['length_ms'])}  at {fmt_ms(at_ms)}  ({how} {TRACK_DIR}/{dst.name})")
    return track


def cmd_add(project: Project, args: Args) -> None:
    name = args.value("--name")
    at = args.value("--at")
    files = args.positionals("gout add FILE... [--name NAME] [--at TIME] [--no-mix]", 1)
    if name and len(files) > 1:
        die("--name works with a single file")
    at_ms = parse_ms(at) if at else 0
    project.record("add " + " ".join(files))
    for f in files:
        ingest(project, Path(f), name, at_ms, args.verbose)
    automix(project, args)


def cmd_ls(project: Project, args: Args) -> None:
    args.positionals("gout ls")
    tracks = project.tracks()
    print(f"proj  {project.get('name')}  {project.rate} Hz  {len(tracks)} track"
          f"{'' if len(tracks) == 1 else 's'}  {TRACK_DIR}/  automix {'on' if project.automix else 'off'}")
    if not tracks:
        print("      no tracks yet — gout add FILE")
        return
    any_solo = any(t["solo"] for t in tracks)
    print(f"{'n':>4}  {'name':<16} {'kind':<4} {'ch':>2}  {'at':<12} {'length':<12} {'file':<12} "
          f"{'trim':<27} {'gain':<7} {'pan':<4} flags")
    for t in tracks:
        a, b = audible(t)
        start, _ = timeline(t)
        trimmed = a > 0 or b < t["length_ms"]
        trim = f"{fmt_ms(a)} > {fmt_ms(b)}" if trimmed else "-"
        flags = ("M" if t["mute"] else "-") + ("S" if t["solo"] else "-")
        if not is_heard(t, any_solo):
            flags += " (silent)"
        print(f"{t['n']:>4}  {t['name']:<16} {t['kind']:<4} {t['channels']:>2}  {fmt_ms(start):<12} "
              f"{fmt_ms(b - a):<12} {fmt_ms(t['length_ms']):<12} {trim:<27} "
              f"{fmt_db(t['gain_db']):<7} {fmt_pan(t['pan']):<4} {flags}")
    master_ms = project.get("master_ms")
    if master_ms and project.master.exists():
        peak = project.get("master_peak")
        print(f"      {MASTER_WAV}  {fmt_ms(int(master_ms))}" + (f"  peak {float(peak):+.1f} dBFS" if peak else ""))
    else:
        print(f"      {MASTER_WAV} not rendered — gout mix")


def cmd_move(project: Project, args: Args) -> None:
    spec, delta = args.positionals("gout move TRACK +TIME | -TIME | TIME  (or =TIME)", 2, 2)
    t = project.track(spec)
    a, _ = audible(t)
    if delta[0] in "+-":
        step = parse_ms(delta[1:])
        offset = t["offset_ms"] + (step if delta[0] == "+" else -step)
    else:
        text = delta[1:] if delta[0] == "=" else delta
        neg = text.startswith("-")
        at = parse_ms(text.lstrip("+-"))
        offset = (-at if neg else at) - a  # place the audible start there
    project.record(f"move {t['name']} {delta}")
    project.update(t["n"], offset_ms=offset)
    start, end = timeline({**t, "offset_ms": offset})
    print(f"move  {t['n']:>2}  {t['name']:<16} at {fmt_ms(start)} -> {fmt_ms(end)}")
    automix(project, args)


def cmd_rm(project: Project, args: Args) -> None:
    delete = args.flag("-D", "--delete")
    (spec,) = args.positionals("gout rm TRACK [-D]", 1, 1)
    t = project.track(spec)
    project.record(f"rm {t['name']}" + (" -D" if delete else ""), undoable=not delete)
    project.delete(t["n"])
    path = project.tracks_dir / t["file"]
    if delete and path.exists():
        path.unlink()
        print(f"rm    {t['name']}  (deleted {TRACK_DIR}/{t['file']})")
    else:
        print(f"rm    {t['name']}  ({TRACK_DIR}/{t['file']} kept; gout add {TRACK_DIR}/{t['file']} brings it back)")
    automix(project, args)


def _toggle(project: Project, args: Args, column: str) -> None:
    pos = args.positionals(f"gout {column} TRACK|all [on|off]", 1, 2)
    spec, state = pos[0], (pos[1] if len(pos) > 1 else None)
    if spec == "all":
        if state is None:
            die(f"gout {column} all needs on or off")
        targets = project.tracks()
    else:
        targets = [project.track(spec)]
    project.record(f"{column} {spec} {state or ''}".strip())
    for t in targets:
        new = on_off(state, t[column])
        project.update(t["n"], **{column: new})
        print(f"{column:<5} {t['n']:>2}  {t['name']:<16} {'on' if new else 'off'}")
    automix(project, args)


def cmd_mute(project: Project, args: Args) -> None:
    _toggle(project, args, "mute")


def cmd_solo(project: Project, args: Args) -> None:
    _toggle(project, args, "solo")


def cmd_gain(project: Project, args: Args) -> None:
    spec, value = args.positionals("gout gain TRACK DB   (e.g. gain 2 -6)", 2, 2)
    t = project.track(spec)
    try:
        gain = float(value.lower().removesuffix("db"))
    except ValueError:
        die(f"bad gain {value!r}, expected a number of dB like -6 or +3.5")
    if not -60 <= gain <= 24:
        die("gain must be between -60 and +24 dB")
    project.record(f"gain {t['name']} {value}")
    project.update(t["n"], gain_db=gain)
    print(f"gain  {t['n']:>2}  {t['name']:<16} {fmt_db(gain)}")
    automix(project, args)


def cmd_pan(project: Project, args: Args) -> None:
    spec, value = args.positionals("gout pan TRACK C | L30 | R30 | -100..100", 2, 2)
    t = project.track(spec)
    v = value.strip().upper()
    match = re.fullmatch(r"([LR])\s*(\d{1,3})", v)
    if v in ("C", "CENTER", "CENTRE", "0"):
        pan = 0.0
    elif match:
        pan = int(match.group(2)) / 100 * (-1 if match.group(1) == "L" else 1)
    else:
        try:
            pan = float(v) / 100
        except ValueError:
            die(f"bad pan {value!r}")
    if not -1 <= pan <= 1:
        die("pan must be between L100 and R100")
    project.record(f"pan {t['name']} {value}")
    project.update(t["n"], pan=pan)
    print(f"pan   {t['n']:>2}  {t['name']:<16} {fmt_pan(pan)}")
    automix(project, args)


def cmd_set(project: Project, args: Args) -> None:
    pos = args.positionals("gout set automix on|off  |  gout set rate HZ", 0, 2)
    if not pos:
        for key in ("name", "rate", "automix", "created"):
            print(f"{key:<8} {project.get(key)}")
        return
    if len(pos) != 2:
        die("usage: gout set KEY VALUE")
    key, value = pos
    if key == "automix":
        value = "on" if on_off(value, 0) else "off"
    elif key == "rate":
        if not value.isdigit() or not 8000 <= int(value) <= 384000:
            die(f"bad sample rate {value!r}")
    else:
        die(f"unknown setting {key!r} (automix, rate)")
    project.record(f"set {key} {value}")
    project.set(key, value)
    print(f"set   {key} {value}")
    if key == "rate":
        automix(project, args)


def cmd_mix(project: Project, args: Args) -> None:
    mp3 = args.flag("--mp3")
    args.positionals("gout mix [--mp3] [-v]")
    mix(project, args.verbose, mp3)


def cmd_undo(project: Project, args: Args) -> None:
    args.positionals("gout undo")
    command = project.undo()
    print(f"undo  {command}")
    automix(project, args)


def cmd_dump(project: Project, args: Args) -> None:
    args.positionals("gout dump")
    print(json.dumps(project.snapshot(), indent=2))


def cmd_rebuild(root_hint: Path | None, args: Args) -> None:
    force = args.flag("-f", "--force")
    args.positionals("gout rebuild [-f]   (run inside the project, or gout -p DIR rebuild)")
    root = (root_hint or Path.cwd()).resolve()
    tracks_dir = root / TRACK_DIR
    if not tracks_dir.is_dir():
        die(f"no {TRACK_DIR}/ directory in {root}")
    db = root / DB_NAME
    if db.exists():
        if not force:
            die(f"{db} exists — pass -f to replace it")
        db.unlink()
    project = Project.create(root, DEFAULT_RATE)
    files = sorted(p for p in tracks_dir.iterdir() if p.suffix.lower() in (".wav", ".mp3"))
    print(f"rebuild  {root}  ({len(files)} files in {TRACK_DIR}/, every track at 0)")
    for path in files:
        ingest(project, path, None, 0, args.verbose)
    if files:
        mix(project)


# --------------------------------------------------------------------------- cut (the 1.x command)

HELP = f"""\
gout {__version__} — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

PROJECT
  gout new NAME [--rate HZ]    create NAME/ with {TRACK_DIR}/ and {DB_NAME} (default {DEFAULT_RATE} Hz)
  gout ls                      list the tracks and the state of {MASTER_WAV}
  gout mix [--mp3] [-v]        render {MASTER_WAV} (32-bit float stereo); --mp3 also writes {MASTER_MP3}
  gout undo                    undo the last change (not a hard trim or rm -D)
  gout dump                    print the project state as JSON
  gout rebuild [-f]            recreate {DB_NAME} from the files in {TRACK_DIR}/, every track at 0
  gout set automix on|off      re-mix after every change (default on);  gout set rate HZ

TRACKS   (TRACK is the number shown by ls, or the track name)
  gout add FILE... [--name N] [--at TIME]   copy wav/mp3 into {TRACK_DIR}/ (other formats become wav)
  gout move TRACK +TIME | -TIME | TIME      nudge later, nudge earlier, or place at a time
  gout rm TRACK [-D]                        drop a track; -D also deletes its file
  gout mute TRACK [on|off]                  toggle mute        (mute all off)
  gout solo TRACK [on|off]                  toggle solo        (solo all off)
  gout gain TRACK DB                        gain 2 -6
  gout pan TRACK C | L30 | R30              balance; every track starts centred, 50/50
  --no-mix on any of these skips the automatic re-mix; -p DIR before a command picks the project.

CUT   (any file, no project needed; `gout INPUT ...` still works as in 1.x)
  gout cut INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE] [-r] [-f] [-n] [-v]
  -st start   -et absolute end   -el length   -fs largest piece that fits the size
  -o output (default <name>_cut.mp3)   -r re-encode for an exact cut   -f overwrite   -n dry run

TIME FORMATS
  00:34:00              HH:MM:SS
  00:34:00.500          HH:MM:SS.mmm      milliseconds after a dot
  00:34:00:500          HH:MM:SS:mmm      milliseconds after a fourth colon
  34:00                 MM:SS
  34                    a bare number is MINUTES, so -st 34 is 34 minutes in
  90s   2.5m   1.5h     explicit units (ms, s, m, h)  — use these for nudges: move 2 +500ms

SIZE FORMATS
  1.99                  a bare number is MB
  700MB  1.99GB  500kB  decimal units, 1 MB = 1 000 000 bytes
  25MiB  1.99GiB        binary units,  1 MiB = 1 048 576 bytes

EXAMPLES
  gout new song && cd song
  gout add drums.mp3 bass.wav              two tracks at 0, {MASTER_WAV} mixed
  gout add vocals.wav --at 00:00:08        a track placed 8 s in
  gout move 3 +250ms                       nudge it a quarter second later
  gout solo 3 && gout mix --mp3            hear it alone, bounce an mp3
  gout cut show.mp3 -st 00:34:00 -fs 1.99  1.99 MB of audio starting at 34:00

HOW THE MIX WORKS
  Every track is decoded, trimmed, delayed to its position, panned to stereo and summed
  with ffmpeg's amix (normalize off, so adding a track never turns the others down).
  {MASTER_WAV} is 32-bit float, so a hot sum cannot clip there; the peak is reported.

HOW -fs WORKS
  The length is estimated from the bitrate, cut, then measured and retried until the
  file lands between 97% and 100% of the limit — never over it.

NOTES
  Needs ffmpeg and ffprobe on PATH. Cutting an mp3 copies the stream, so it snaps to a
  frame boundary (~26 ms); -r re-encodes for the exact millisecond. wav cuts are exact.
  Everything else is the Python standard library — the project state lives in {DB_NAME}
  (sqlite), the audio in {TRACK_DIR}/ is never modified by moves or trims.
"""


class Parser(argparse.ArgumentParser):
    """Prints the hand-written instruction page instead of argparse's."""

    def format_help(self) -> str:
        return HELP

    def format_usage(self) -> str:
        return "usage: gout cut INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE]\n" \
               "       gout -h    for the full instructions\n"

    def error(self, message: str) -> "NoReturn":  # noqa: F821
        die(f"{message}\n{self.format_usage().rstrip()}")


def build_parser() -> argparse.ArgumentParser:
    p = Parser(
        prog="gout cut",
        description="Cut a piece out of an .mp3 by time or by target file size.",
        add_help=False,
    )
    p.add_argument("-h", "--help", action="help", help="show this help message and exit")
    p.add_argument("input", type=Path, help="source .mp3")
    p.add_argument("-o", "--output", type=Path, help="output file (default: <name>_cut.mp3)")
    p.add_argument("-st", "--start", default="0", metavar="TIME",
                   help="start time from the beginning of the file (default: 0)")

    end = p.add_mutually_exclusive_group()
    end.add_argument("-et", "--end", metavar="TIME",
                     help="absolute end time, measured from the beginning of the file")
    end.add_argument("-el", "--length", metavar="TIME",
                     help="length of the cut, measured from the start time")
    end.add_argument("-fs", "--file-size", metavar="SIZE",
                     help="cut as much as fits in this file size")

    p.add_argument("-r", "--reencode", action="store_true",
                   help="re-encode instead of copying (sample-accurate cut, slower)")
    p.add_argument("-f", "--force", action="store_true", help="overwrite the output file")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be cut and stop")
    p.add_argument("-v", "--verbose", action="store_true", help="show ffmpeg commands and size passes")
    p.add_argument("-V", "--version", action="version", version=f"gout {__version__}")
    return p


def cmd_cut(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)

    src: Path = args.input
    if not src.is_file():
        die(f"no such file: {src}")

    try:
        start = parse_time(args.start)
        end = parse_time(args.end) if args.end else None
        length = parse_time(args.length) if args.length else None
        limit = parse_size(args.file_size) if args.file_size else None
    except ValueError as exc:
        die(str(exc))

    info = probe(src)
    total = info["duration"]
    if start >= total:
        die(f"start {fmt_time(start)} is past the end of the file ({fmt_time(total)})")
    remaining = total - start

    duration: float | None = None
    if end is not None:
        if end <= start:
            die(f"end {fmt_time(end)} is not after start {fmt_time(start)}")
        duration = min(end - start, remaining)
    elif length is not None:
        if length <= 0:
            die("length must be greater than zero")
        duration = min(length, remaining)

    dst: Path = args.output or src.with_name(f"{src.stem}_cut{src.suffix or '.mp3'}")
    if dst.resolve() == src.resolve():
        die("output would overwrite the input file — pass -o")
    if dst.exists() and not args.force and not args.dry_run:
        die(f"{dst} already exists — pass -f to overwrite")

    print(f"in    {src}  ({fmt_time(total)}, {fmt_size(src.stat().st_size)}, "
          f"{round(info['bitrate'] / 1000)} kbps {info['codec']})")
    print(f"cut   {fmt_time(start)} -> " + (
        fmt_time(start + duration) if duration is not None
        else (f"whatever fits in {fmt_size(limit)}" if limit else fmt_time(total))))

    if args.dry_run:
        if limit:
            estimate = min((limit - TAG_ALLOWANCE) * 8 / info["bitrate"], remaining)
            print(f"      ~{fmt_time(estimate)} at this bitrate (estimate, not verified)")
        print(f"out   {dst}  (dry run, nothing written)")
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    codec = info["codec"] if info["codec"].startswith("pcm_") else "mp3"
    if limit is not None:
        duration, size = cut_to_size(src, dst, start, limit, remaining,
                                     args.reencode, info["bitrate"], args.verbose)
        print(f"      {fmt_time(start)} -> {fmt_time(start + duration)} "
              f"({fmt_time(duration)}, {size / limit:.1%} of the limit)")
    else:
        cut(src, dst, start, duration, args.reencode, info["bitrate"], args.verbose, codec)
        size = dst.stat().st_size

    print(f"out   {dst}  ({fmt_size(size)})")


# --------------------------------------------------------------------------- dispatch

PROJECT_COMMANDS = {
    "add": cmd_add, "ls": cmd_ls, "move": cmd_move, "rm": cmd_rm,
    "mute": cmd_mute, "solo": cmd_solo, "gain": cmd_gain, "pan": cmd_pan,
    "set": cmd_set, "mix": cmd_mix, "undo": cmd_undo, "dump": cmd_dump,
}
FREE_COMMANDS = {"new": cmd_new, "rebuild": cmd_rebuild}
ALIASES = {"list": "ls", "mv": "move", "remove": "rm", "render": "mix", "bounce": "mix"}


def run(argv: list[str], project: Project | None = None) -> int:
    """Run one command line. `project` is the open project when called from the UI."""
    root_hint: Path | None = None
    if "-p" in argv or "--project" in argv:
        i = argv.index("-p") if "-p" in argv else argv.index("--project")
        if i + 1 >= len(argv):
            die("-p needs a directory")
        root_hint = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
        if not root_hint.is_dir():
            die(f"no such directory: {root_hint}")

    if not argv:
        found = project or Project.find(root_hint)
        if found is None:
            print(HELP, end="")
            return 0
        need_tools()
        cmd_ls(found, Args([]))
        return 0

    head, rest = argv[0], argv[1:]
    head = ALIASES.get(head, head)
    if head in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    if head in ("-V", "--version", "version"):
        print(f"gout {__version__}")
        return 0

    need_tools()
    if head in FREE_COMMANDS:
        FREE_COMMANDS[head](root_hint, Args(rest))
        return 0
    if head in PROJECT_COMMANDS:
        found = project or Project.find(root_hint)
        if found is None:
            die(f"not inside a gout project (no {DB_NAME} here or above) — gout new NAME")
        PROJECT_COMMANDS[head](found, Args(rest))
        return 0
    if head == "cut":
        cmd_cut(rest)
        return 0
    if Path(head).is_file() or head.startswith("-"):
        cmd_cut(argv)  # the 1.x form: gout INPUT [-st ...]
        return 0
    die(f"unknown command {head!r}, and no such file — gout -h")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return run(argv)
    except GoutError as exc:
        print(f"gout: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
