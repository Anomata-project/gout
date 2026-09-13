#!/usr/bin/env python3
"""gout — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

    gout new song && cd song
    gout add drums.mp3 bass.wav        # copied into master/, master.wav is mixed
    gout move 2 +1.5s                  # nudge the bass 1.5 s later, mix again
    gout cut show.mp3 -st 34 -fs 1.99  # the 1.x cutter, unchanged
"""
from __future__ import annotations

import argparse
import cmath
import contextlib
import datetime as dt
import io
import math
import os
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from array import array
from pathlib import Path

__version__ = "2.0.0"

DB_NAME = "gout.db"
SIDECAR = "gout.json"  # the readable copy of the state, rewritten after every change
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


ENV_RATE = 50    # peaks per second of audio
ENV_SR = 8000    # decode rate for the envelope: 160 samples per peak
LEVELS = "▁▂▃▄▅▆▇█"  # 6 dB per step, top step is -6 dBFS and up


def compute_envelope(path: Path) -> bytes:
    """Peak per 20 ms window, 0..128, from a mono 8-bit decode. Empty when ffmpeg fails."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", str(ENV_SR), "-f", "u8", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return b""
    data, n = result.stdout, ENV_SR // ENV_RATE
    peaks = bytearray()
    for i in range(0, len(data), n):
        chunk = data[i:i + n]  # max/min on bytes run in C, so this is quick even for hours
        peaks.append(max(max(chunk) - 128, 128 - min(chunk)))
    return bytes(peaks)


SPEC_BANDS = 40     # log-spaced 20 Hz .. 20 kHz
SPEC_N = 4096       # fft size
SPEC_WINDOWS = 8    # windows spread over up to a minute from the middle of the file
SPEC_SR = 44100
SPEC_RANGE = 60.0   # dB below the loudest band that still shows


def fft(x: list[complex]) -> list[complex]:
    """In-place style radix-2 FFT, pure Python; len(x) must be a power of two."""
    n = len(x)
    y = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            y[i], y[j] = y[j], y[i]
    length = 2
    while length <= n:
        ang = -2 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        half = length // 2
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(i, i + half):
                u, v = y[k], y[k + half] * w
                y[k], y[k + half] = u + v, u - v
                w *= wlen
        length <<= 1
    return y


def compute_spectrum(path: Path, duration: float) -> bytes:
    """Average spectrum of a file as SPEC_BANDS bytes: 255 is the loudest band, 0 is
    SPEC_RANGE dB under it. A minute from the middle of the file, a few Hann windows."""
    start = max(0.0, duration / 2 - 30)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", "60", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", str(SPEC_SR), "-f", "s16le", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return b""
    data = array("h")
    data.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    if len(data) < SPEC_N:
        return b""
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * n / (SPEC_N - 1)) for n in range(SPEC_N)]
    edges = [20 * (1000 ** (k / SPEC_BANDS)) for k in range(SPEC_BANDS + 1)]
    bins = []
    for k in range(SPEC_BANDS):
        lo = max(1, int(edges[k] * SPEC_N / SPEC_SR))
        hi = max(lo + 1, int(edges[k + 1] * SPEC_N / SPEC_SR))
        bins.append((min(lo, SPEC_N // 2 - 1), min(hi, SPEC_N // 2)))
    power = [0.0] * SPEC_BANDS
    step = max(SPEC_N, (len(data) - SPEC_N) // max(1, SPEC_WINDOWS - 1))
    starts = list(range(0, len(data) - SPEC_N + 1, step))[:SPEC_WINDOWS]
    for s0 in starts:
        frame = [complex(data[s0 + n] * hann[n]) for n in range(SPEC_N)]
        spectrum = fft(frame)
        mags = [abs(v) ** 2 for v in spectrum[:SPEC_N // 2]]
        for k, (lo, hi) in enumerate(bins):
            power[k] += sum(mags[lo:hi]) / (hi - lo)
    db = [10 * math.log10(v / len(starts) + 1e-9) for v in power]
    top = max(db)
    return bytes(max(0, min(255, round((v - top + SPEC_RANGE) / SPEC_RANGE * 255))) for v in db)


def level_char(peak: int) -> str:
    if peak <= 0:
        return LEVELS[0]
    db = 20 * math.log10(min(peak, 128) / 128)
    return LEVELS[max(0, min(7, 7 + math.ceil(db / 6)))]


def envelope_char(env: bytes, lo_ms: float, hi_ms: float) -> str:
    """The block for the loudest peak between two file times; █ when no envelope exists."""
    if not env:
        return CELL_AUDIBLE
    i0 = max(0, min(len(env) - 1, int(lo_ms * ENV_RATE / 1000)))
    i1 = max(i0 + 1, min(len(env), math.ceil(hi_ms * ENV_RATE / 1000)))
    return level_char(max(env[i0:i1]))


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


# --------------------------------------------------------------------------- mp3 frames
#
# ffmpeg's own -ss on an mp3 with -c copy lands 90-115 ms late (measured, any length,
# CBR or VBR), which is too sloppy to keep a hard-trimmed track aligned. So hard trims
# of mp3 walk the frames in Python and copy whole frames: the removed head is then a
# known number of frames, and the ffmpeg pass afterwards only adds the Xing header and
# carries the tags. What ffmpeg's decoder skips at the start of a file depends on the
# LAME tag: delay+529 samples when the tag exists, nothing otherwise; the new file gets
# a tag with delay 0, so the audio moves by exactly the old delay (or -529 samples).

MP3_KBPS = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),  # MPEG-1 layer III
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),      # MPEG-2 / 2.5
}
MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
MP3_DECODER_DELAY = 529


def id3v2_size(data: bytes) -> int:
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)


def mp3_lame_delay(data: bytes) -> int:
    """Encoder delay from the LAME/Lavc/Lavf tag in the Xing/Info frame, or -1 without one."""
    head = data[id3v2_size(data):][:4096]
    for tag in (b"Xing", b"Info"):
        i = head.find(tag)
        if i < 0:
            continue
        flags = int.from_bytes(head[i + 4:i + 8], "big")
        j = i + 8 + (4 if flags & 1 else 0) + (4 if flags & 2 else 0) \
            + (100 if flags & 4 else 0) + (4 if flags & 8 else 0)
        b = head[j + 21:j + 24]
        if len(b) == 3 and head[j:j + 4] in (b"LAME", b"Lavc", b"Lavf"):
            return (b[0] << 4) | (b[1] >> 4)
        return -1
    return -1


def mp3_index(data: bytes) -> tuple[array, array, int, int]:
    """Walk the layer III frames: (positions, sizes, sample_rate, samples_per_frame).

    ID3 tags and the Xing/Info/VBRI frame are skipped; junk is scanned past.
    """
    pos, end = id3v2_size(data), len(data)
    if end >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    positions, sizes = array("Q"), array("H")
    sr = spf = 0
    while pos + 4 <= end:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        b1, b2 = data[pos + 1], data[pos + 2]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        bri, sri, pad = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
        if version == 1 or layer != 1 or bri in (0, 15) or sri == 3:
            pos += 1
            continue
        rate = MP3_RATES[version][sri]
        this_spf = 1152 if version == 3 else 576
        size = this_spf // 8 * MP3_KBPS[1 if version == 3 else 2][bri] * 1000 // rate + pad
        if pos + size > end:
            break
        if not sr:
            sr, spf = rate, this_spf
        if not positions:
            frame = data[pos:pos + size]
            if b"Xing" in frame or b"Info" in frame or b"VBRI" in frame:
                pos += size  # the VBR header frame carries no audio
                continue
        positions.append(pos)
        sizes.append(size)
        pos += size
    return positions, sizes, sr, spf


def mp3_frame_cut(src: Path, dst: Path, a_ms: int, b_ms: int, verbose: bool) -> tuple[int, int, int]:
    """Copy the frames covering [a_ms, b_ms) of src to dst without re-encoding.

    Returns (head_ms, kept_from_ms, kept_to_ms): head_ms is how much decoded audio
    disappeared from the front, so the track's timeline offset must grow by it.
    """
    data = src.read_bytes()
    positions, sizes, sr, spf = mp3_index(data)
    if len(positions) < 2:
        die(f"could not read the mp3 frames of {src.name}")
    frame_ms = spf * 1000 / sr
    k0 = max(0, min(len(positions) - 1, int(a_ms / frame_ms)))
    k1 = max(k0 + 1, min(len(positions), math.ceil(b_ms / frame_ms)))
    raw = dst.with_name(dst.name + ".frames")
    span = positions[k1 - 1] + sizes[k1 - 1] - positions[k0]
    if span == sum(sizes[k0:k1]):  # contiguous, the normal case
        raw.write_bytes(data[positions[k0]:positions[k0] + span])
    else:
        with raw.open("wb") as fh:
            for k in range(k0, k1):
                fh.write(data[positions[k]:positions[k] + sizes[k]])
    try:
        run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                   "-f", "mp3", "-i", str(raw), "-map", "1:a", "-map_metadata", "0",
                   "-c:a", "copy", "-write_xing", "1", "-id3v2_version", "3", str(dst)], verbose)
    finally:
        raw.unlink(missing_ok=True)
    delay = mp3_lame_delay(data)
    # decoded_cut(y) == decoded_src(y + head): the old file skipped delay+529 samples
    # (or none without a tag), the new one skips 529, so the head is a bit less than k0 frames
    head = k0 * spf - (delay if delay >= 0 else -MP3_DECODER_DELAY)
    return round(head * 1000 / sr), round(k0 * frame_ms), round(k1 * frame_ms)


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
    solo        INTEGER NOT NULL DEFAULT 0,
    eq          TEXT NOT NULL DEFAULT '',
    eq_on       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS envelopes (
    file  TEXT PRIMARY KEY,
    size  INTEGER NOT NULL,
    mtime REAL NOT NULL,
    rate  INTEGER NOT NULL,
    peaks BLOB NOT NULL,
    lufs  REAL,
    tp    REAL,
    lra   REAL,
    spectrum BLOB
);
CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    command  TEXT NOT NULL,
    undoable INTEGER NOT NULL DEFAULT 1,
    snapshot TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT '[]'
);
"""

TRACK_COLUMNS = ("n", "name", "file", "kind", "length_ms", "channels", "sample_rate",
                 "offset_ms", "in_ms", "out_ms", "gain_db", "pan", "mute", "solo", "eq", "eq_on")


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
        if "created" not in {r[1] for r in self.conn.execute("PRAGMA table_info(history)")}:
            with self.conn:  # databases from before the column existed
                self.conn.execute("ALTER TABLE history ADD COLUMN created TEXT NOT NULL DEFAULT '[]'")
        if "eq" not in {r[1] for r in self.conn.execute("PRAGMA table_info(tracks)")}:
            with self.conn:
                self.conn.execute("ALTER TABLE tracks ADD COLUMN eq TEXT NOT NULL DEFAULT ''")
                self.conn.execute("ALTER TABLE tracks ADD COLUMN eq_on INTEGER NOT NULL DEFAULT 1")
        if "lufs" not in {r[1] for r in self.conn.execute("PRAGMA table_info(envelopes)")}:
            with self.conn:
                for col in ("lufs", "tp", "lra"):
                    self.conn.execute(f"ALTER TABLE envelopes ADD COLUMN {col} REAL")
        if "spectrum" not in {r[1] for r in self.conn.execute("PRAGMA table_info(envelopes)")}:
            with self.conn:
                self.conn.execute("ALTER TABLE envelopes ADD COLUMN spectrum BLOB")
        old = self.get("automix")  # the setting was called automix before 2.0.0 final
        if old is not None:
            if self.get("autorender") is None:
                self.set("autorender", old)
            self.unset("automix")

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
        project.set("autorender", "on")
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
    def autorender(self) -> bool:
        return (self.get("autorender") or "on") != "off"

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

    # ---- envelopes

    def envelope(self, name: str, path: Path) -> bytes:
        """Cached peaks for a file in master/ (or master.wav); recomputed when the file changed."""
        try:
            st = path.stat()
        except OSError:
            return b""
        row = self.conn.execute("SELECT size, mtime, rate, peaks FROM envelopes WHERE file = ?",
                                (name,)).fetchone()
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime and row["rate"] == ENV_RATE:
            return row["peaks"]
        peaks = compute_envelope(path)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO envelopes (file, size, mtime, rate, peaks)"
                              " VALUES (?, ?, ?, ?, ?)", (name, st.st_size, st.st_mtime, ENV_RATE, peaks))
        return peaks

    def loudness(self, name: str, path: Path) -> dict | None:
        """Cached integrated loudness, true peak and LRA of a file; measured on first use."""
        self.envelope(name, path)  # makes sure the row exists and is fresh
        row = self.conn.execute("SELECT lufs, tp, lra FROM envelopes WHERE file = ?", (name,)).fetchone()
        if row is None:
            return None
        if row["lufs"] is not None:
            return {"i": row["lufs"], "tp": row["tp"], "lra": row["lra"]}
        m = measure_loudness(path)
        if m is None:
            return None
        with self.conn:
            self.conn.execute("UPDATE envelopes SET lufs = ?, tp = ?, lra = ? WHERE file = ?",
                              (m["i"], m["tp"], m["lra"], name))
        return m

    def spectrum(self, name: str, path: Path) -> bytes:
        """Cached average spectrum of a file; computed on first use, dropped when the file changes."""
        self.envelope(name, path)
        row = self.conn.execute("SELECT spectrum FROM envelopes WHERE file = ?", (name,)).fetchone()
        if row is None:
            return b""
        if row["spectrum"] is not None:
            return row["spectrum"]
        try:
            duration = probe(path)["duration"]
        except GoutError:
            return b""
        spec = compute_spectrum(path, duration)
        with self.conn:
            self.conn.execute("UPDATE envelopes SET spectrum = ? WHERE file = ?", (spec, name))
        return spec

    def forget_envelope(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM envelopes WHERE file = ?", (name,))

    # ---- history

    def snapshot(self) -> dict:
        settings = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM project")
                    if not r["key"].startswith("ui_")}  # ui_ keys are preferences, not state
        return {"project": settings, "tracks": self.tracks()}

    def record(self, command: str, undoable: bool = True) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO history (ts, command, undoable, snapshot) VALUES (?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), command, int(undoable),
                 json.dumps(self.snapshot())),
            )

    def created(self, files: list[str]) -> None:
        """Note files the command being recorded copied into master/ (undo may delete them)."""
        if files:
            with self.conn:
                self.conn.execute("UPDATE history SET created = ? WHERE id = (SELECT MAX(id) FROM history)",
                                  (json.dumps(files),))

    def document(self) -> dict:
        return {"gout": __version__, **self.snapshot()}

    def sync_json(self) -> None:
        """Write gout.json next to the database whenever the state it describes changed."""
        text = json.dumps(self.document(), indent=2) + "\n"
        path = self.root / SIDECAR
        try:
            if path.exists() and path.read_text() == text:
                return
            tmp = path.with_name(SIDECAR + ".part")
            tmp.write_text(text)
            tmp.replace(path)
        except OSError as exc:
            print(f"      could not write {SIDECAR}: {exc}", file=sys.stderr)

    def reorder(self, files: list[str]) -> None:
        """Number the tracks so those in `files` come first, in that order."""
        tracks = self.tracks()
        ranked = sorted(tracks, key=lambda t: (files.index(t["file"]) if t["file"] in files
                                               else len(files) + t["n"]))
        with self.conn:
            self.conn.execute("UPDATE tracks SET n = -n")
            for new, t in enumerate(ranked, 1):
                self.conn.execute("UPDATE tracks SET n = ? WHERE n = ?", (new, -t["n"]))

    def restore(self, snap: dict) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tracks")
            for t in snap["tracks"]:
                cols = [c for c in TRACK_COLUMNS if c in t]
                self.conn.execute(
                    f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple(t[c] for c in cols),
                )
            self.conn.execute("DELETE FROM project WHERE key NOT LIKE 'ui_%'")
            for k, v in snap["project"].items():
                self.conn.execute("INSERT INTO project (key, value) VALUES (?, ?)", (k, v))

    def undo(self) -> str:
        row = self.conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            die("nothing to undo")
        if not row["undoable"]:
            die(f"cannot undo '{row['command']}': it rewrote or deleted an audio file")
        snap = json.loads(row["snapshot"])
        self.restore(snap)
        still_used = {t["file"] for t in snap["tracks"]}
        for name in json.loads(row["created"] or "[]"):  # only copies gout made, never the user's files
            path = self.tracks_dir / name
            if name not in still_used and path.exists():
                path.unlink()
        with self.conn:
            self.conn.execute("DELETE FROM history WHERE id = ?", (row["id"],))
        return row["command"]


# --------------------------------------------------------------------------- master settings

MASTER_DEFAULTS = {
    "lufs": "off", "ceiling": "-1", "gain": "0", "fadein": "0", "fadeout": "0", "head": "0", "tail": "0",
    "bits": "32f", "mp3": "320k", "title": "", "artist": "", "album": "", "year": "", "comment": "",
}
TAG_KEYS = ("title", "artist", "album", "year", "comment")
BITS_CODEC = {"32f": "pcm_f32le", "24": "pcm_s24le", "16": "pcm_s16le"}


def setting(project: "Project", key: str) -> str:
    return project.get(key) or MASTER_DEFAULTS.get(key, "")


def parse_setting(key: str, value: str) -> tuple[str, str]:
    """Validate a `set` value; returns (canonical key, stored string)."""
    v = value.strip()
    if key in ("autorender", "automix"):
        return "autorender", "on" if on_off(v, 0) else "off"
    if key == "rate":
        if not v.isdigit() or not 8000 <= int(v) <= 384000:
            die(f"bad sample rate {value!r}")
        return key, v
    if key == "lufs":
        if v.lower() in ("off", "no", "none", "0"):
            return key, "off"
        try:
            target = float(v)
        except ValueError:
            die(f"bad loudness target {value!r}: -14, -16, -23 or off")
        if not -40 <= target <= -5:
            die("the loudness target must be between -40 and -5 LUFS")
        return key, f"{target:g}"
    if key in ("ceiling", "gain"):
        try:
            x = float(v.lower().removesuffix("dbtp").removesuffix("db"))
        except ValueError:
            die(f"bad {key} {value!r}")
        lo, hi = (-20, 0) if key == "ceiling" else (-60, 24)
        if not lo <= x <= hi:
            die(f"{key} must be between {lo} and {hi} dB")
        return key, f"{x:g}"
    if key in ("fadein", "fadeout", "head", "tail"):
        if v.lower() in ("off", "0", "none"):
            return key, "0"
        return key, str(parse_ms(v))
    if key == "bits":
        if v.lower() not in BITS_CODEC:
            die("bits must be 32f, 24 or 16")
        return key, v.lower()
    if key == "mp3":
        if not re.fullmatch(r"\d{2,3}k|v[0-9]", v.lower()):
            die("mp3 quality is a bitrate like 320k or 192k, or v0 .. v9 (VBR, v0 is best)")
        return key, v.lower()
    if key in TAG_KEYS:
        return key, value
    die(f"unknown setting {key!r} — gout set (with nothing after it) lists them")


def tag_args(project: "Project") -> list[str]:
    out: list[str] = []
    for key in TAG_KEYS:
        value = setting(project, key)
        if value:
            out += ["-metadata", f"{'date' if key == 'year' else key}={value}"]
    return out


# --------------------------------------------------------------------------- eq
#
# A track's eq is one line of bands:  hp80  lp12k  hp80/24  +3@200  -4@2.5k/3  ls100:+2  hs8k:-3
# (cuts with a slope in dB per octave, peaks as gain@frequency/Q, shelves as frequency:gain).

EQ_SLOPES = {6: (1,), 12: (2,), 18: (2, 1), 24: (2, 2), 36: (2, 2, 2), 48: (2, 2, 2, 2)}
EQ_SHELF_Q = 0.707
EQ_MAX_BANDS = 16
EQ_SYNTAX = "hp80  lp12k  hp80/24  +3@200  -4@2.5k/3  ls100:+2  hs8k:-3"
EQ_PRESETS = {  # a preset name stands for these bands; they can be mixed with bands of your own
    "voice":   ("hp80 -3@250/1.5 +2@3k/1.2 hs10k:+1", "spoken word: no rumble, less box, more presence"),
    "podcast": ("hp80 -2@300/1.5 +2@2.5k/1.2 lp16k", "voice, a touch softer, nothing above 16 kHz"),
    "warm":    ("ls200:+2 hs6k:-2", "a little more low end, a little less top"),
    "air":     ("hs10k:+3", "sheen above 10 kHz"),
    "bright":  ("hs4k:+3", "more top from 4 kHz up"),
    "mud":     ("-4@250/1.2", "takes the mud out around 250 Hz"),
    "clean":   ("hp40", "just the rumble under 40 Hz gone"),
    "phone":   ("hp300 lp3.4k", "the telephone effect"),
    "bass":    ("hp30 ls100:+3", "bass instruments: subsonics gone, body up"),
    "kick":    ("hp40 +3@60/1.5 -3@400/1.5 +2@4k/1.5", "kick drum: thump, less cardboard, click"),
    "guitar":  ("hp100 -2@300/1.5 +2@2.5k/1.5", "guitars sit better: less low mud, more bite"),
    "flat":    ("", "no eq at all"),
}


def parse_hz(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(k?)", text.lower())
    if not m:
        raise ValueError(f"bad frequency {text!r} (80, 2.5k, 12k)")
    f = float(m.group(1)) * (1000 if m.group(2) else 1)
    if not 10 <= f <= 22000:
        raise ValueError(f"frequency {text} is outside 10 Hz .. 22 kHz")
    return f


def fmt_hz(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


def parse_band(token: str) -> dict:
    t = token.lower()
    m = re.fullmatch(r"(hp|lp)(\d+(?:\.\d+)?k?)(?:/(\d+))?", t)
    if m:
        slope = int(m.group(3) or 12)
        if slope not in EQ_SLOPES:
            raise ValueError(f"slope {slope} in {token!r}: use 6, 12, 18, 24, 36 or 48 dB per octave")
        return {"type": m.group(1), "f": parse_hz(m.group(2)), "slope": slope}
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)@(\d+(?:\.\d+)?k?)(?:/(\d+(?:\.\d+)?))?", t)
    if m:
        g, q = float(m.group(1)), float(m.group(3) or 1)
        if not -24 <= g <= 24:
            raise ValueError(f"gain {g:+g} in {token!r}: keep it within ±24 dB")
        if not 0.1 <= q <= 20:
            raise ValueError(f"Q {q:g} in {token!r}: use 0.1 .. 20")
        return {"type": "peak", "f": parse_hz(m.group(2)), "g": g, "q": q}
    m = re.fullmatch(r"(ls|hs)(\d+(?:\.\d+)?k?):([+-]?\d+(?:\.\d+)?)", t)
    if m:
        g = float(m.group(3))
        if not -24 <= g <= 24:
            raise ValueError(f"gain {g:+g} in {token!r}: keep it within ±24 dB")
        return {"type": m.group(1), "f": parse_hz(m.group(2)), "g": g}
    raise ValueError(f"bad band {token!r}; bands look like  {EQ_SYNTAX}  (or a preset: eq presets)")


def fmt_band(b: dict) -> str:
    if b["type"] in ("hp", "lp"):
        return f"{b['type']}{fmt_hz(b['f'])}" + (f"/{b['slope']}" if b["slope"] != 12 else "")
    if b["type"] == "peak":
        return f"{b['g']:+g}@{fmt_hz(b['f'])}" + (f"/{b['q']:g}" if b["q"] != 1 else "")
    return f"{b['type']}{fmt_hz(b['f'])}:{b['g']:+g}"


def parse_eq(text: str) -> list[dict]:
    """Bands from a line of tokens, in a fixed order: hp, lp, then the rest as written.
    A preset name in the line stands for its bands."""
    tokens: list[str] = []
    for tok in text.split():
        if tok.lower() in EQ_PRESETS:
            tokens += EQ_PRESETS[tok.lower()][0].split()
        else:
            tokens.append(tok)
    bands = [parse_band(tok) for tok in tokens]
    cuts = {}
    rest = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            cuts[b["type"]] = b  # the last hp or lp wins
        else:
            rest.append(b)
    if len(rest) > EQ_MAX_BANDS:
        raise ValueError(f"more than {EQ_MAX_BANDS} bands")
    return [cuts[k] for k in ("hp", "lp") if k in cuts] + rest


def fmt_eq(bands: list[dict]) -> str:
    return " ".join(fmt_band(b) for b in bands)


def eq_filters(bands: list[dict]) -> list[str]:
    out: list[str] = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            name = "highpass" if b["type"] == "hp" else "lowpass"
            out += [f"{name}=f={b['f']:g}:poles={poles}" for poles in EQ_SLOPES[b["slope"]]]
        elif b["type"] == "peak":
            out.append(f"equalizer=f={b['f']:g}:t=q:w={b['q']:g}:g={b['g']:g}")
        else:
            name = "lowshelf" if b["type"] == "ls" else "highshelf"
            out.append(f"{name}=f={b['f']:g}:t=q:w={EQ_SHELF_Q}:g={b['g']:g}")
    return out


def track_eq(t: dict) -> list[dict]:
    """The stored bands of a track, if its eq is on."""
    if not t.get("eq") or not t.get("eq_on", 1):
        return []
    try:
        return parse_eq(t["eq"])
    except ValueError:
        return []


def biquad(kind: str, f: float, fs: float, q: float = EQ_SHELF_Q, g: float = 0.0) -> tuple:
    """RBJ cookbook coefficients, the same family ffmpeg's biquads use."""
    w0 = 2 * math.pi * min(f, fs * 0.499) / fs
    cw, sw = math.cos(w0), math.sin(w0)
    if kind == "lp1":
        a1 = -math.exp(-w0)
        return (1 + a1, 0.0, 0.0, 1.0, a1, 0.0)
    if kind == "hp1":
        a1 = -math.exp(-w0)
        b0 = (1 - a1) / 2
        return (b0, -b0, 0.0, 1.0, a1, 0.0)
    alpha = sw / (2 * q)
    if kind == "lp2":
        return ((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if kind == "hp2":
        return ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    A = 10 ** (g / 40)
    if kind == "peak":
        return (1 + alpha * A, -2 * cw, 1 - alpha * A, 1 + alpha / A, -2 * cw, 1 - alpha / A)
    r = 2 * math.sqrt(A) * alpha
    if kind == "ls":
        return (A * ((A + 1) - (A - 1) * cw + r), 2 * A * ((A - 1) - (A + 1) * cw),
                A * ((A + 1) - (A - 1) * cw - r), (A + 1) + (A - 1) * cw + r,
                -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - r)
    return (A * ((A + 1) + (A - 1) * cw + r), -2 * A * ((A - 1) + (A + 1) * cw),  # hs
            A * ((A + 1) + (A - 1) * cw - r), (A + 1) - (A - 1) * cw + r,
            2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - r)


def eq_sections(bands: list[dict], fs: float) -> list[tuple]:
    out = []
    for b in bands:
        if b["type"] in ("hp", "lp"):
            for poles in EQ_SLOPES[b["slope"]]:
                out.append(biquad(f"{b['type']}{poles}", b["f"], fs))
        elif b["type"] == "peak":
            out.append(biquad("peak", b["f"], fs, b["q"], b["g"]))
        else:
            out.append(biquad(b["type"], b["f"], fs, EQ_SHELF_Q, b["g"]))
    return out


def eq_response(bands: list[dict], fs: float, freqs: list[float]) -> list[float]:
    """Gain in dB of the whole eq at each frequency."""
    sections = eq_sections(bands, fs)
    out = []
    for f in freqs:
        z1 = cmath.exp(-1j * 2 * math.pi * min(f, fs * 0.499) / fs)
        z2 = z1 * z1
        db = 0.0
        for b0, b1, b2, a0, a1, a2 in sections:
            h = abs((b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2))
            db += 20 * math.log10(h) if h > 1e-9 else -180
        out.append(db)
    return out


EQ_GUTTER = 4
EQ_TICKS = ((20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"),
            (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k"))


def render_eq(project: "Project", t: dict, width: int, height: int = 8,
              spectrum: bytes | None = None) -> list[tuple[str, str, str]]:
    """Rows of (text, classes, kind) plotting a track's eq curve from 20 Hz to 20 kHz.

    kind is head, graph or axis. Classes: a the curve, z the 0 dB line, x the spectrum.
    Half blocks give two levels per row; the dB range fits the curve, at least ±6.
    """
    gw = max(12, width - EQ_GUTTER)
    freqs = [20 * (1000 ** (c / (gw - 1))) for c in range(gw)]
    try:
        bands = parse_eq(t["eq"]) if t["eq"] else []
    except ValueError:
        bands = []
    curve = eq_response(bands, project.rate, freqs)
    # the range follows the boosts and cuts of peaks and shelves; cut slopes run off the bottom
    loudest = max([abs(b["g"]) for b in bands if b["type"] in ("peak", "ls", "hs")] + [0.0])
    span = max(12, min(24, math.ceil((loudest + 1.5) / 6) * 6))
    sub = 2 * height

    def ysub(db: float) -> int:
        db = max(-span, min(span, db))
        return max(0, min(sub - 1, round((span - db) / (2 * span) * (sub - 1))))

    cells = [[" "] * gw for _ in range(height)]
    classes = [[" "] * gw for _ in range(height)]
    zero_row = ysub(0) // 2
    if spectrum:  # background: one bar per column, dim
        for c in range(gw):
            level = spectrum[min(len(spectrum) - 1, c * len(spectrum) // gw)] / 255  # 0..1 of the range
            top_sub = round((1 - level) * (sub - 1))
            for row in range(height):
                if 2 * row >= top_sub:
                    cells[row][c], classes[row][c] = "░", "x"
    for c in range(gw):
        if classes[zero_row][c] == " ":
            cells[zero_row][c], classes[zero_row][c] = "─", "z"
    ys = [ysub(v) for v in curve]
    for c in range(gw):
        lo, hi = (ys[c], ys[c]) if c == 0 else (min(ys[c - 1], ys[c]), max(ys[c - 1], ys[c]))
        for row in range(height):
            top, bot = lo <= 2 * row <= hi, lo <= 2 * row + 1 <= hi
            if top or bot:
                cells[row][c] = "█" if top and bot else ("▀" if top else "▄")
                classes[row][c] = "a"
    state = "" if not t["eq"] else ("  (off)" if not t["eq_on"] else "")
    rows = [(f"eq   {t['n']:>2} {t['name'][:9]:<9} {t['eq'] or 'flat'}{state}"
             + ("  ░ spectrum" if spectrum else "")[:width], "", "head")]
    for row in range(height):
        label = f"{span:+d}" if row == 0 else (f"{-span:+d}" if row == height - 1 else ("0" if row == zero_row else ""))
        rows.append((f"{label:>{EQ_GUTTER - 1}} " + "".join(cells[row]),
                     " " * EQ_GUTTER + "".join(classes[row]), "graph"))
    axis = [" "] * gw
    last = -2
    for f, text in EQ_TICKS:
        c = round(math.log(f / 20) / math.log(1000) * (gw - 1))
        c = max(0, min(gw - len(text), c - len(text) // 2))
        if c > last + 1:
            axis[c:c + len(text)] = list(text)
            last = c + len(text) - 1
    rows.append((" " * EQ_GUTTER + "".join(axis), "", "axis"))
    return rows


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


def track_steps(project: Project, t: dict) -> list[str] | None:
    """The filters that put one track on the timeline as the mix hears it: format, soft
    trim, position, gain, pan. None when nothing of it is audible."""
    a, b = audible(t)
    start = t["offset_ms"] + a
    trim_start, delay = a, start
    if start < 0:  # the head hangs before the timeline: cut it, no delay
        trim_start, delay = a - start, 0
    if b <= trim_start:
        return None
    steps = [f"aformat=sample_rates={project.rate}:sample_fmts=fltp"]
    if trim_start > 0 or b < t["length_ms"]:
        steps.append(f"atrim=start={trim_start / 1000:.3f}:end={b / 1000:.3f}")
        steps.append("asetpts=PTS-STARTPTS")
    if delay > 0:
        steps.append(f"adelay={delay}:all=1")
    steps += eq_filters(track_eq(t))
    if t["gain_db"]:
        steps.append(f"volume={t['gain_db']:.2f}dB")
    steps.append(pan_filter(t["channels"], t["pan"]))
    return steps


def build_graph(project: Project, tracks: list[dict]) -> tuple[list[Path], str, list[dict]]:
    any_solo = any(t["solo"] for t in tracks)
    inputs: list[Path] = []
    chains: list[str] = []
    used: list[dict] = []
    for t in tracks:
        if not is_heard(t, any_solo):
            continue
        steps = track_steps(project, t)
        if steps is None:
            continue
        i = len(inputs)
        inputs.append(project.tracks_dir / t["file"])
        used.append(t)
        chains.append(f"[{i}:a]" + ",".join(steps) + f"[t{i}]")
    if not inputs:
        return [], "", []
    labels = "".join(f"[t{i}]" for i in range(len(inputs)))
    chains.append(f"{labels}amix=inputs={len(inputs)}:normalize=0:duration=longest"
                  f":dropout_transition=0[mix]")
    return inputs, ";".join(chains), used


def measure_loudness(path: Path, target: float = -23.0, ceiling: float = -1.0) -> dict | None:
    """Integrated loudness, true peak, LRA and threshold as loudnorm's first pass sees them."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"loudnorm=I={target}:TP={ceiling}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
    if result.returncode != 0 or not match:
        return None
    data = json.loads(match.group(0))
    return {"i": float(data["input_i"]), "tp": float(data["input_tp"]), "lra": float(data["input_lra"]),
            "thresh": float(data["input_thresh"]), "offset": float(data.get("target_offset", 0))}


def fmt_lufs(m: dict | None) -> str:
    if not m or m["i"] == float("-inf") or m["i"] < -70:
        return "silent"
    return f"{m['i']:.1f} LUFS  LRA {m['lra']:.1f}  peak {m['tp']:+.1f} dBTP"


def mix(project: Project, verbose: bool = False, mp3: bool = False) -> None:
    tracks = project.tracks()
    inputs, graph, used = build_graph(project, tracks)
    if not inputs:
        if project.master.exists():
            project.master.unlink()
        for key in ("master_ms", "master_lufs", "master_tp", "master_lra"):
            project.unset(key)
        print("mix   nothing audible" + (" — master.wav removed" if tracks else "")
              + ("" if tracks else " (no tracks yet)"))
        return

    # pass 1: the sum, master gain and fades, float at the project rate
    post: list[str] = []
    gain = float(setting(project, "gain"))
    if gain:
        post.append(f"volume={gain:.2f}dB")
    fade_in, fade_out = int(setting(project, "fadein")), int(setting(project, "fadeout"))
    end_ms = max(timeline(t)[1] for t in used)
    if fade_in > 0:
        post.append(f"afade=t=in:d={fade_in / 1000:.3f}")
    if fade_out > 0:
        post.append(f"afade=t=out:st={max(0, end_ms - fade_out) / 1000:.3f}:d={fade_out / 1000:.3f}")
    if post:
        graph = graph[:-len("[mix]")] + "[sum];[sum]" + ",".join(post) + "[mix]"
    raw = project.root / "master.raw.part.wav"
    final = project.root / "master.part.wav"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", "[mix]", "-ar", str(project.rate), "-c:a", "pcm_f32le", str(raw)]
    how = ""
    target = None
    try:
        run_quiet(cmd, verbose)

        # pass 2: loudness, padding, format and tags
        lufs = setting(project, "lufs")
        target = None if lufs == "off" else float(lufs)
        ceiling = float(setting(project, "ceiling"))
        measured = measure_loudness(raw, target if target is not None else -23.0, ceiling)
        duration = probe(raw)["duration"]
        chain: list[str] = []
        if target is not None and measured is None:
            how = "loudness could not be measured, left as is"
        elif target is not None:
            if measured["i"] < -70:
                how = "silent, nothing to normalise"
            elif duration < 3:
                g = min(target - measured["i"], ceiling - measured["tp"])
                chain.append(f"volume={g:.2f}dB")
                how = f"gain {g:+.1f} dB (plain gain: under 3 s)"
            else:
                lra = max(7, min(50, math.ceil(measured["lra"]) + 1))
                # loudnorm reads a measured LRA of exactly 0 as "unknown" and refuses linear mode
                chain.append(f"loudnorm=I={target}:TP={ceiling}:LRA={lra}:measured_I={measured['i']}"
                             f":measured_TP={measured['tp']}:measured_LRA={max(measured['lra'], 0.01)}"
                             f":measured_thresh={measured['thresh']}:offset={measured['offset']}"
                             f":linear=true:print_format=json")
                how = "loudnorm"
        head, tail = int(setting(project, "head")), int(setting(project, "tail"))
        if head > 0:
            chain.append(f"adelay={head}:all=1")
        if tail > 0:
            chain.append(f"apad=pad_dur={tail / 1000:.3f}")
        bits = setting(project, "bits")
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y", "-i", str(raw)]
        if chain:
            cmd += ["-af", ",".join(chain)]
        cmd += ["-ar", str(project.rate)]
        if bits == "16":
            cmd += ["-dither_method", "triangular"]
        cmd += ["-c:a", BITS_CODEC[bits], *tag_args(project), str(final)]
        if verbose:
            print("  $ " + " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            problems = [l for l in result.stderr.splitlines() if "rror" in l or "nvalid" in l]
            die("ffmpeg failed:\n" + "\n".join(problems[-12:]))
        if how == "loudnorm":
            match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
            info = json.loads(match.group(0)) if match else {}
            mode = info.get("normalization_type", "?")
            delta = float(info.get("output_i", 0)) - float(info.get("input_i", 0))
            how = (f"linear gain {delta:+.1f} dB" if mode == "linear"
                   else f"dynamic: the ceiling stopped a plain gain of {target - measured['i']:+.1f} dB")
        final.replace(project.master)
    finally:
        for tmp in (raw, final):
            if tmp.exists():
                tmp.unlink()

    length_ms = round(probe(project.master)["duration"] * 1000)
    got = measure_loudness(project.master)
    project.set("master_ms", str(length_ms))
    for key, field in (("master_lufs", "i"), ("master_tp", "tp"), ("master_lra", "lra")):
        project.set(key, "" if got is None else f"{got[field]:.2f}")
    project.envelope(MASTER_WAV, project.master)
    skipped = len(tracks) - len(used)
    note = f"  ({len(used)} of {len(tracks)} tracks)" if skipped else ""
    print(f"mix   {MASTER_WAV}  {fmt_ms(length_ms)}  {fmt_lufs(got)}{note}")
    if how:
        print(f"      {how}")
    if got and target is None and got["tp"] > 0:
        print("      true peak above 0 dBTP: it will clip on export — set lufs -14, or lower a gain")
    if mp3:
        out = project.root / MASTER_MP3
        quality = setting(project, "mp3")
        q = ["-q:a", quality[1:]] if quality.startswith("v") else ["-b:a", quality]
        run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(project.master),
                   "-c:a", "libmp3lame", *q, "-id3v2_version", "3", *tag_args(project), str(out)], verbose)
        print(f"      {MASTER_MP3}  {fmt_size(out.stat().st_size)}  ({quality})")


def autorender(project: Project, args: "Args") -> None:
    """Re-render master.wav after a change, unless -N was given or the setting is off."""
    if args.no_mix:
        return
    if project.autorender:
        mix(project)


# --------------------------------------------------------------------------- command args


class Args:
    """Tiny flag parser. Values like -2s or +1.5s stay positional; unknown -x flags fail."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        self.no_mix = self.flag("--no-mix", "-N")
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
    rate_txt = args.value("--rate", "-R", default=str(DEFAULT_RATE))
    (name,) = args.positionals("gout new NAME [-R HZ]", 1, 1)
    if not rate_txt.isdigit() or not 8000 <= int(rate_txt) <= 384000:
        die(f"bad sample rate {rate_txt!r}")
    root = Path.cwd() if name == "." else Path(name)
    if (root / DB_NAME).exists():
        die(f"{root / DB_NAME} already exists")
    project = Project.create(root, int(rate_txt))
    project.sync_json()
    print(f"new   {project.root}  ({project.rate} Hz, tracks go in {TRACK_DIR}/)")


def ingest(project: Project, src: Path, name: str | None, at_ms: int, verbose: bool) -> tuple[dict, bool]:
    """Register src as a track; copies it into master/ unless it already lives there.

    Returns (track, created) where created says whether a new file was written.
    """
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
    if in_place and not any(t["file"] == src.name for t in project.tracks()):
        taken = {t["name"] for t in project.tracks()}
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", name or src.stem).strip("-.") or "track"
        track_name, i = base, 2
        while track_name in taken:
            track_name, i = f"{base}-{i}", i + 1
        dst = src
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
    project.envelope(dst.name, dst)  # so the timeline can draw it without a pause later
    how = "kept in" if dst == src else ("copied to" if ext == suffix else "converted to")
    print(f"add   {track['n']:>2}  {track['name']:<16} {kind}  {info['channels']}ch  {info['sample_rate']} Hz"
          f"  {fmt_ms(track['length_ms'])}  at {fmt_ms(at_ms)}  ({how} {TRACK_DIR}/{dst.name})")
    return track, dst != src


def cmd_add(project: Project, args: Args) -> None:
    name = args.value("--name", "-n")
    at = args.value("--at", "-a")
    files = args.positionals("gout add FILE... [-n NAME] [-a TIME] [-N]", 1)
    if name and len(files) > 1:
        die("--name works with a single file")
    at_ms = parse_ms(at) if at else 0
    project.record("add " + " ".join(files))
    made: list[str] = []
    for f in files:
        track, created = ingest(project, Path(f), name, at_ms, args.verbose)
        if created:
            made.append(track["file"])
    project.created(made)
    autorender(project, args)


def cmd_scan(project: Project, args: Args) -> None:
    """Register files that appeared in master/ by hand; report tracks whose file is gone."""
    args.positionals("gout scan [-N]")
    tracks = project.tracks()
    known = {t["file"] for t in tracks}
    files = sorted(f for f in project.tracks_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in (".wav", ".mp3")
                   and not f.name.startswith(".") and ".part" not in f.name)
    new = [f for f in files if f.name not in known]
    missing = [t for t in tracks if not (project.tracks_dir / t["file"]).exists()]
    if not new and not missing:
        print(f"scan  {TRACK_DIR}/ matches {DB_NAME}: {len(files)} file{'' if len(files) == 1 else 's'},"
              f" nothing new, nothing missing")
        return
    if new:
        project.record("scan " + " ".join(f.name for f in new))
        for f in new:
            ingest(project, f, None, 0, args.verbose)
    for t in missing:
        print(f"scan  missing  {t['n']:>2}  {t['name']:<16} {TRACK_DIR}/{t['file']} is gone"
              f"  (gout rm {t['n']} drops the track, or put the file back)")
    if new:
        autorender(project, args)


def cmd_ls(project: Project, args: Args) -> None:
    args.positionals("gout ls")
    tracks = project.tracks()
    print(f"proj  {project.get('name')}  {project.rate} Hz  {len(tracks)} track"
          f"{'' if len(tracks) == 1 else 's'}  {TRACK_DIR}/  autorender {'on' if project.autorender else 'off'}")
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
        if t["eq"]:
            flags += f"  eq {t['eq']}" + ("" if t["eq_on"] else " (off)")
        print(f"{t['n']:>4}  {t['name']:<16} {t['kind']:<4} {t['channels']:>2}  {fmt_ms(start):<12} "
              f"{fmt_ms(b - a):<12} {fmt_ms(t['length_ms']):<12} {trim:<27} "
              f"{fmt_db(t['gain_db']):<7} {fmt_pan(t['pan']):<4} {flags}")
    master_ms = project.get("master_ms")
    if master_ms and project.master.exists():
        lufs, tp = project.get("master_lufs"), project.get("master_tp")
        loud = f"  {float(lufs):.1f} LUFS  peak {float(tp):+.1f} dBTP" if lufs and tp else ""
        print(f"      {MASTER_WAV}  {fmt_ms(int(master_ms))}{loud}")
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
    autorender(project, args)


TRIM_USAGE = ("gout trim TRACK [-st TIME] [-et TIME | -el TIME] [-c]          soft: file untouched\n"
              "       gout trim TRACK -H [-st ..] [-et ..|-el ..] [-r]           hard: rewrite the file\n"
              "       times are measured from the start of the track's own file")


def cmd_trim(project: Project, args: Args) -> None:
    hard = args.flag("--hard", "-H")
    clear = args.flag("--clear", "-c")
    reencode = args.flag("-r", "--reencode")
    st, et, el = args.value("-st", "--start"), args.value("-et", "--end"), args.value("-el", "--length")
    (spec,) = args.positionals(TRIM_USAGE, 1, 1)
    if et and el:
        die("-et and -el are exclusive\n" + TRIM_USAGE)
    t = project.track(spec)
    length = t["length_ms"]
    a, b = audible(t)
    if clear:
        a, b = 0, length
    if st:
        a = parse_ms(st)
    if et:
        b = parse_ms(et)
    elif el:
        b = a + parse_ms(el)
    b = min(b, length)
    if a < 0 or a >= length:
        die(f"in point {fmt_ms(a)} is outside the file ({fmt_ms(length)} long)")
    if b <= a:
        die(f"out point {fmt_ms(b)} is not after the in point {fmt_ms(a)}")

    if not (hard or clear or st or et or el):
        trimmed = a > 0 or b < length
        print(f"trim  {t['n']:>2}  {t['name']:<16} " + (f"{fmt_ms(a)} > {fmt_ms(b)}  of {fmt_ms(length)}"
              if trimmed else f"none (whole file, {fmt_ms(length)})") + "  soft")
        return

    if not hard:
        project.record(f"trim {t['name']} {fmt_ms(a)}>{fmt_ms(b)}")
        project.update(t["n"], in_ms=a, out_ms=None if b >= length else b)
        start, end = timeline({**t, "in_ms": a, "out_ms": b})
        print(f"trim  {t['n']:>2}  {t['name']:<16} {fmt_ms(a)} > {fmt_ms(b)}  soft, {fmt_ms(b - a)} audible"
              f"  at {fmt_ms(start)} -> {fmt_ms(end)}")
        autorender(project, args)
        return

    if a == 0 and b >= length:
        die("nothing to cut: the whole file is already the audible part (soft-trim first, or give -st/-et)")
    src = project.tracks_dir / t["file"]
    tmp = src.with_name(f"{src.stem}.part{src.suffix}")
    info = probe(src)
    project.record(f"trim {t['name']} --hard {fmt_ms(a)}>{fmt_ms(b)}", undoable=False)
    try:
        if t["kind"] == "mp3" and not reencode:
            head, snap_a, snap_b = mp3_frame_cut(src, tmp, a, b, args.verbose)
            how = f"mp3 frames {fmt_ms(snap_a)} > {fmt_ms(snap_b)}, not re-encoded"
        else:
            codec = info["codec"] if info["codec"].startswith("pcm_") else "mp3"
            cut(src, tmp, a / 1000, (b - a) / 1000, reencode, info["bitrate"], args.verbose, codec)
            head = a
            how = "re-encoded, exact" if t["kind"] == "mp3" else "wav, exact"
        tmp.replace(src)
    finally:
        tmp.unlink(missing_ok=True)
    new_len = round(probe(src)["duration"] * 1000)
    project.envelope(src.name, src)
    offset = t["offset_ms"] + head
    project.update(t["n"], offset_ms=offset, in_ms=0, out_ms=None, length_ms=new_len)
    start, end = timeline({**t, "offset_ms": offset, "in_ms": 0, "out_ms": None, "length_ms": new_len})
    print(f"trim  {t['n']:>2}  {t['name']:<16} {fmt_ms(a)} > {fmt_ms(b)}  hard ({how})")
    print(f"      {TRACK_DIR}/{t['file']}  {fmt_ms(length)} -> {fmt_ms(new_len)}"
          f"  at {fmt_ms(start)} -> {fmt_ms(end)}  (position kept; not undoable)")
    autorender(project, args)


def cmd_rm(project: Project, args: Args) -> None:
    delete = args.flag("-D", "--delete")
    (spec,) = args.positionals("gout rm TRACK [-D]", 1, 1)
    t = project.track(spec)
    project.record(f"rm {t['name']}" + (" -D" if delete else ""), undoable=not delete)
    project.delete(t["n"])
    path = project.tracks_dir / t["file"]
    if delete and path.exists():
        path.unlink()
        project.forget_envelope(t["file"])
        print(f"rm    {t['name']}  (deleted {TRACK_DIR}/{t['file']})")
    else:
        print(f"rm    {t['name']}  ({TRACK_DIR}/{t['file']} kept; gout add {TRACK_DIR}/{t['file']} brings it back)")
    autorender(project, args)


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
    autorender(project, args)


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
    autorender(project, args)


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
    autorender(project, args)


SET_USAGE = "gout set KEY VALUE   (gout set alone lists the keys and their values)"


EQ_USAGE = (f"gout eq TRACK [BANDS... | PRESET | on | off | clear]\n       bands: {EQ_SYNTAX}\n"
            f"       presets: {' '.join(EQ_PRESETS)}   (eq presets explains them)\n"
            "       hp/lp: cut with a slope in dB per octave (12 by default); +3@200: a peak of +3 dB at 200 Hz,\n"
            "       /3 sets its Q; ls100:+2 and hs8k:-3 are shelves")


def eq_line(t: dict) -> str:
    text = t["eq"] or "flat"
    if t["eq"] and not t["eq_on"]:
        text += "  (off: bypassed, eq TRACK on brings it back)"
    return f"eq    {t['n']:>2}  {t['name']:<16} {text}"


def cmd_eq(project: Project, args: Args) -> None:
    pos = args.positionals(EQ_USAGE, 1)
    if pos[0].lower() in ("presets", "preset", "list"):
        print("presets  a name stands for these bands; use it alone or with bands of your own, eq 3 voice +1@5k")
        for name, (bands, what) in EQ_PRESETS.items():
            print(f"  {name:<8} {bands or 'flat':<38} {what}")
        return
    t = project.track(pos[0])
    words = pos[1:]
    if not words:
        print(eq_line(t))
        width = min(100, shutil.get_terminal_size((100, 24)).columns)
        spectrum = project.spectrum(t["file"], project.tracks_dir / t["file"]) or None
        for text, _, kind in render_eq(project, t, width - 6, spectrum=spectrum)[1:]:
            print("      " + text)
        if spectrum:
            print("      ░ the track's own spectrum, loudest band at the top")
        return
    if words == ["off"]:
        project.record(f"eq {t['name']} off")
        project.update(t["n"], eq_on=0)
    elif words == ["on"]:
        project.record(f"eq {t['name']} on")
        project.update(t["n"], eq_on=1)
    elif words == ["clear"]:
        project.record(f"eq {t['name']} clear")
        project.update(t["n"], eq="", eq_on=1)
    else:
        try:
            bands = parse_eq(" ".join(words))
        except ValueError as exc:
            die(f"{exc}\n{EQ_USAGE}")
        project.record(f"eq {t['name']} {' '.join(words)}")
        project.update(t["n"], eq=fmt_eq(bands), eq_on=1)
    print(eq_line(project.track(str(t["n"]))))
    autorender(project, args)


def _cut(project: Project, args: Args, kind: str) -> None:
    pos = args.positionals(f"gout {kind} TRACK HZ [SLOPE] | off     e.g. {kind} 3 {'80' if kind == 'hp' else '12k'}"
                           f"  ({kind} 3 80 24 for 24 dB per octave)", 2, 3)
    t = project.track(pos[0])
    try:
        bands = [b for b in parse_eq(t["eq"]) if b["type"] != kind]
        if pos[1].lower() != "off":
            slope = int(pos[2]) if len(pos) > 2 else 12
            if slope not in EQ_SLOPES:
                raise ValueError(f"slope {pos[2]}: use 6, 12, 18, 24, 36 or 48 dB per octave")
            bands.append({"type": kind, "f": parse_hz(pos[1]), "slope": slope})
        bands = parse_eq(fmt_eq(bands))  # canonical order
    except ValueError as exc:
        die(str(exc))
    project.record(f"{kind} {t['name']} {' '.join(pos[1:])}")
    project.update(t["n"], eq=fmt_eq(bands), eq_on=1)
    print(eq_line(project.track(str(t["n"]))))
    autorender(project, args)


def cmd_hp(project: Project, args: Args) -> None:
    _cut(project, args, "hp")


def cmd_lp(project: Project, args: Args) -> None:
    _cut(project, args, "lp")


def cmd_set(project: Project, args: Args) -> None:
    pos = args.positionals(SET_USAGE, 0, 2)
    if not pos:
        hints = {
            "autorender": "render master.wav after every change",
            "lufs": "loudness target: -14 (streaming) -16 (Apple) -23 (broadcast) or off",
            "ceiling": "true-peak ceiling in dBTP for the loudness step",
            "gain": "master gain in dB, before the loudness step",
            "fadein": "e.g. 500ms", "fadeout": "e.g. 3s", "head": "silence before, e.g. 500ms",
            "tail": "silence after, e.g. 2s", "bits": "32f | 24 | 16 (dithered)",
            "mp3": "bounce quality: 320k, 192k, v0 .. v9",
        }
        print(f"{'name':<11} {project.get('name')}")
        print(f"{'rate':<11} {project.rate}")
        print(f"{'autorender':<11} {'on' if project.autorender else 'off':<14} {hints['autorender']}")
        for key in MASTER_DEFAULTS:
            value = setting(project, key)
            if key in ("fadein", "fadeout", "head", "tail") and value != "0":
                value = fmt_ms(int(value))
            elif key in TAG_KEYS:
                value = value or "-"
            print(f"{key:<11} {value:<14} {hints.get(key, '')}")
        return
    if len(pos) != 2:
        die(SET_USAGE)
    key, value = parse_setting(pos[0].lower(), pos[1])
    project.record(f"set {key} {value}")
    project.set(key, value)
    shown = fmt_ms(int(value)) if key in ("fadein", "fadeout", "head", "tail") and value != "0" else value
    print(f"set   {key} {shown or '(cleared)'}")
    if key != "autorender":
        autorender(project, args)


def cmd_stats(project: Project, args: Args) -> None:
    args.positionals("gout stats")
    tracks = project.tracks()
    if not tracks:
        die("the project has no tracks yet — gout add FILE")
    print(f"stats {'n':>2}  {'name':<16} {'file LUFS':>9} {'LRA':>5} {'peak dBTP':>9} {'gain':>7} {'-> LUFS':>8}  flags")
    for t in tracks:
        m = project.loudness(t["file"], project.tracks_dir / t["file"])
        flags = ("M" if t["mute"] else "-") + ("S" if t["solo"] else "-")
        if m is None or m["i"] < -70:
            print(f"{t['n']:>8}  {t['name']:<16} {'silent':>9} {'':>5} {'':>9} {fmt_db(t['gain_db']):>7} {'':>8}  {flags}")
            continue
        print(f"{t['n']:>8}  {t['name']:<16} {m['i']:>9.1f} {m['lra']:>5.1f} {m['tp']:>+9.1f} "
              f"{fmt_db(t['gain_db']):>7} {m['i'] + t['gain_db']:>8.1f}  {flags}")
    lufs, tp, lra = (project.get(k) for k in ("master_lufs", "master_tp", "master_lra"))
    target, ceiling = setting(project, "lufs"), setting(project, "ceiling")
    goal = f"target {target} LUFS under {ceiling} dBTP" if target != "off" else "no loudness target (set lufs -14)"
    if lufs and tp and lra and project.master.exists():
        print(f"{'':>8}  {MASTER_WAV:<16} {float(lufs):>9.1f} {float(lra):>5.1f} {float(tp):>+9.1f} {'':>7} {'':>8}  {goal}")
    else:
        print(f"{'':>8}  {MASTER_WAV:<16} not rendered — gout mix   ({goal})")


def cmd_mix(project: Project, args: Args) -> None:
    mp3 = args.flag("--mp3", "-3")
    args.positionals("gout mix [-3] [-v]")
    mix(project, args.verbose, mp3)


STEMS_DIR = "stems"


def cmd_stems(project: Project, args: Args) -> None:
    """One file per track, processed as in the mix, all the same length from 0:00."""
    audible_only = args.flag("--audible", "-A")
    pos = args.positionals(f"gout stems [DIR] [-A]   (default {STEMS_DIR}/ in the project; -A: only what the mix hears)", 0, 1)
    tracks = project.tracks()
    if not tracks:
        die("the project has no tracks yet — gout add FILE")
    any_solo = any(t["solo"] for t in tracks)
    plans = []
    for t in tracks:
        if audible_only and not is_heard(t, any_solo):
            continue
        steps = track_steps(project, t)
        if steps:
            plans.append((t, steps))
    if not plans:
        die("nothing to export: no track has audible material" + (" the mix hears" if audible_only else ""))
    out_dir = Path(pos[0]).expanduser() if pos else project.root / STEMS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    end_ms = max(1, max(timeline(t)[1] for t, _ in plans))
    bits = setting(project, "bits")
    print(f"stems {out_dir}  {len(plans)} track{'' if len(plans) == 1 else 's'}, {fmt_ms(end_ms)} each"
          f"  ({bits}, {project.rate} Hz, stereo)")
    for t, steps in plans:
        name = f"{t['n']:02d}-{t['name']}.wav"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(project.tracks_dir / t["file"]),
               "-af", ",".join(steps + [f"apad=whole_dur={end_ms / 1000:.3f}"]), "-ar", str(project.rate)]
        if bits == "16":
            cmd += ["-dither_method", "triangular"]
        cmd += ["-c:a", BITS_CODEC[bits], str(out_dir / name)]
        run_quiet(cmd, args.verbose)
        state = "" if is_heard(t, any_solo) else "  (muted or not soloed in the mix)"
        print(f"      {name:<26} {fmt_size((out_dir / name).stat().st_size):>10}{state}")
    master_bits = [k for k in ("gain", "fadein", "fadeout", "head", "tail") if setting(project, k) not in ("0", "")]
    lufs = setting(project, "lufs")
    if master_bits or lufs != "off":
        what = ", ".join(master_bits + (["the loudness target"] if lufs != "off" else []))
        print(f"      not in the stems: master {what}. Summed at unity they give the mix before the master step.")
    else:
        print(f"      summed at unity they give {MASTER_WAV} exactly")


def apply_document(project: Project, data: dict, settings: bool = True, tracks: bool = True,
                   verbose: bool = False) -> tuple[int, int, list[str]]:
    """Apply a gout.json document to the project. Returns (settings, tracks applied, warnings)."""
    warnings: list[str] = []
    n_settings = 0
    if settings:
        for key, value in (data.get("project") or {}).items():
            if key in ("name", "created") or key.startswith(("master_", "ui_")):
                continue
            if key == "automix":
                key = "autorender"
            if key not in ("rate", "autorender") and key not in MASTER_DEFAULTS:
                continue  # something a newer or older gout knew about
            try:
                k, v = parse_setting(key, str(value))
            except GoutError as exc:
                warnings.append(f"{key}: {exc}")
                continue
            project.set(k, v)
            n_settings += 1
    n_tracks = 0
    if tracks:
        order: list[str] = []
        for item in data.get("tracks") or []:
            if not isinstance(item, dict):
                continue
            file, name = item.get("file"), item.get("name")
            current = project.tracks()
            t = next((x for x in current if x["file"] == file), None) or \
                next((x for x in current if x["name"] == name), None)
            if t is None:
                path = project.tracks_dir / file if file else None
                if path is not None and path.is_file():
                    t, _ = ingest(project, path, name, 0, verbose)
                else:
                    warnings.append(f"{name or file}: no such file in {TRACK_DIR}/, skipped")
                    continue
            fields: dict = {}
            try:
                if item.get("offset_ms") is not None:
                    fields["offset_ms"] = int(item["offset_ms"])
                if item.get("in_ms") is not None:
                    fields["in_ms"] = max(0, min(int(item["in_ms"]), t["length_ms"] - 1))
                if "out_ms" in item:
                    out = item["out_ms"]
                    fields["out_ms"] = None if out is None or int(out) >= t["length_ms"] else int(out)
                if item.get("gain_db") is not None:
                    fields["gain_db"] = max(-60.0, min(24.0, float(item["gain_db"])))
                if item.get("pan") is not None:
                    fields["pan"] = max(-1.0, min(1.0, float(item["pan"])))
                for flag in ("mute", "solo", "eq_on"):
                    if item.get(flag) is not None:
                        fields[flag] = 1 if item[flag] else 0
                if item.get("eq") is not None:
                    try:
                        fields["eq"] = fmt_eq(parse_eq(str(item["eq"])))
                    except ValueError as exc:
                        warnings.append(f"{t['name']}: eq ignored ({exc})")
            except (TypeError, ValueError) as exc:
                warnings.append(f"{t['name']}: bad value ({exc}), skipped")
                continue
            in_ms = fields.get("in_ms", t["in_ms"])
            out_ms = fields.get("out_ms", t["out_ms"])
            if out_ms is not None and out_ms <= in_ms:
                fields["out_ms"] = None
            if fields:
                project.update(t["n"], **fields)
            order.append(t["file"])
            n_tracks += 1
        if order:
            project.reorder(order)
    return n_settings, n_tracks, warnings


def read_document(path: Path) -> dict:
    if not path.is_file():
        die(f"no such file: {path}")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or not ("project" in data or "tracks" in data):
        die(f"{path} is not a gout document (expected the shape of gout dump)")
    return data


def cmd_import(project: Project, args: Args) -> None:
    only_settings = args.flag("--settings", "-s")
    only_tracks = args.flag("--tracks", "-t")
    (path,) = args.positionals("gout import FILE.json [-s | -t]   (-s settings only, -t tracks only)", 1, 1)
    data = read_document(Path(path).expanduser())
    project.record(f"import {Path(path).name}")
    n_settings, n_tracks, warnings = apply_document(project, data, settings=not only_tracks,
                                                    tracks=not only_settings, verbose=args.verbose)
    print(f"import {path}  {n_settings} setting{'' if n_settings == 1 else 's'},"
          f" {n_tracks} track{'' if n_tracks == 1 else 's'}")
    for w in warnings:
        print(f"      {w}")
    autorender(project, args)


def cmd_undo(project: Project, args: Args) -> None:
    args.positionals("gout undo")
    command = project.undo()
    print(f"undo  {command}")
    autorender(project, args)


def save_as(project: Project, name: str) -> Project:
    """Copy the whole project (database, master/, renders) to a new directory and open it.

    A bare name lands next to the current project; a path with a slash goes where it says.
    """
    dst = Path(name).expanduser()
    if not dst.is_absolute() and "/" not in name:
        dst = project.root.parent / name
    dst = dst.resolve()
    if dst == project.root:
        die("that is this project")
    if dst.exists():
        die(f"{dst} already exists")
    if project.root in dst.parents:
        die("the copy would end up inside this project — give a name for a sibling, or a path")
    dst.mkdir(parents=True)
    shutil.copy2(project.db_path, dst / DB_NAME)
    shutil.copytree(project.tracks_dir, dst / TRACK_DIR)
    for extra in (MASTER_WAV, MASTER_MP3):
        if (project.root / extra).exists():
            shutil.copy2(project.root / extra, dst / extra)
    copy = Project(dst)
    copy.set("name", dst.name)
    copy.set("created", dt.datetime.now().isoformat(timespec="seconds"))
    copy.sync_json()
    return copy


def cmd_saveas(project: Project, args: Args) -> None:
    (name,) = args.positionals("gout saveas NAME | PATH   (a bare name goes next to this project)", 1, 1)
    copy = save_as(project, name)
    files = sum(1 for _ in copy.tracks_dir.iterdir())
    print(f"saved {copy.root}  ({files} track file{'' if files == 1 else 's'} copied; this project is untouched)")


def cmd_dump(project: Project, args: Args) -> None:
    args.positionals("gout dump")
    print(json.dumps(project.document(), indent=2))


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
    print(f"rebuild  {root}  ({len(files)} files in {TRACK_DIR}/)")
    for path in files:
        ingest(project, path, None, 0, args.verbose)
    side = root / SIDECAR
    if side.is_file():
        n_settings, n_tracks, warnings = apply_document(project, read_document(side), verbose=args.verbose)
        print(f"      positions, trims and settings restored from {SIDECAR}"
              f" ({n_settings} settings, {n_tracks} tracks)")
        for w in warnings:
            print(f"      {w}")
    else:
        print(f"      no {SIDECAR} found: every track at 0, default settings")
    project.sync_json()
    if files and project.autorender:
        mix(project)


# --------------------------------------------------------------------------- timeline view

LABEL_W = 15  # " n name      MS"
TICK_STEPS = (100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
              600000, 900000, 1800000, 3600000, 7200000, 18000000)
CELL_AUDIBLE, CELL_SILENT, CELL_TRIMMED, CELL_ZERO, CELL_MASTER = "█", "▒", "░", "│", "━"


def render_timeline(project: Project, width: int, styled: bool = False) -> list[tuple[str, str, str, str]]:
    """Rows of (label, cells, kind, classes) for a timeline `width` columns wide.

    kind is axis, ruler, track, master or note. Every column of a track shows the
    peak level of that slice of audio as a block ▁▂▃▄▅▆▇█ (6 dB per step). classes
    marks each cell: a audible, s audible but muted or not soloed, t soft-trimmed
    away, z the zero line, m master. Plain output draws trimmed material as ░;
    `styled` (the ui) draws its envelope too and dims it.
    """
    tracks = project.tracks()
    tw = max(10, width - LABEL_W - 1)
    master_ms = int(project.get("master_ms") or 0) if project.master.exists() else 0
    if not tracks:
        return [("", "no tracks yet — add FILE", "note", "")]

    t0 = min(0, min(t["offset_ms"] for t in tracks))
    t1 = max(max(t["offset_ms"] + t["length_ms"] for t in tracks), master_ms, t0 + 1000)
    scale = tw / (t1 - t0)  # columns per millisecond

    def col(ms: int) -> int:
        return int((ms - t0) * scale)

    def cols(a: int, b: int) -> tuple[int, int]:
        start = max(0, min(tw - 1, col(a)))
        end = max(start + 1, min(tw, math.ceil((b - t0) * scale)))
        return start, end

    def paint(cells: list[str], classes: list[str], env: bytes, offset: int, lo: int, hi: int,
              first: int, last: int, cls: str) -> None:
        """Envelope blocks for columns first..last-1, clipped to file time lo..hi."""
        for c in range(first, last):
            lo_ms = max(lo, t0 + c / scale - offset)
            hi_ms = min(hi, t0 + (c + 1) / scale - offset)
            cells[c] = envelope_char(env, lo_ms, hi_ms)
            classes[c] = cls

    step = next((s for s in TICK_STEPS if s * scale >= 9), TICK_STEPS[-1])
    decimals = 0 if step >= 1000 else (2 if step == 250 else 1)
    labels, ruler = [" "] * tw, ["─"] * tw
    last_end = -1
    tick = math.ceil(t0 / step) * step
    while tick <= t1:
        c = col(tick)
        if 0 <= c < tw:
            ruler[c] = "┼"
            text = fmt_short(tick, decimals)
            if c > last_end and c + len(text) <= tw:
                labels[c:c + len(text)] = list(text)
                last_end = c + len(text)
        tick += step
    zero = col(0) if t0 < 0 else -1
    rows = [("", "".join(labels), "axis", ""), ("", "".join(ruler), "ruler", "")]

    any_solo = any(t["solo"] for t in tracks)
    for t in tracks:
        cells, classes = [" "] * tw, [" "] * tw
        if 0 <= zero < tw:
            cells[zero], classes[zero] = CELL_ZERO, "z"
        env = project.envelope(t["file"], project.tracks_dir / t["file"])
        off, length = t["offset_ms"], t["length_ms"]
        fs, fe = cols(off, off + length)
        if styled:
            paint(cells, classes, env, off, 0, length, fs, fe, "t")
        else:
            cells[fs:fe], classes[fs:fe] = [CELL_TRIMMED] * (fe - fs), ["t"] * (fe - fs)
        a, b = audible(t)
        if b > a:
            s_, e_ = cols(off + a, off + b)
            paint(cells, classes, env, off, a, b, s_, e_, "a" if is_heard(t, any_solo) else "s")
        flags = ("M" if t["mute"] else " ") + ("S" if t["solo"] else " ")
        rows.append((f"{t['n']:>2} {t['name'][:9]:<9} {flags}", "".join(cells), "track", "".join(classes)))

    label = f"   {MASTER_WAV}"[:LABEL_W].ljust(LABEL_W)
    if master_ms:
        cells, classes = [" "] * tw, [" "] * tw
        env = project.envelope(MASTER_WAV, project.master)
        s_, e_ = cols(0, master_ms)
        paint(cells, classes, env, 0, 0, master_ms, s_, e_, "m")
        rows.append((label, "".join(cells), "master", "".join(classes)))
    else:
        rows.append((label, "not rendered — mix", "note", ""))
    return rows


def cmd_view(project: Project, args: Args) -> None:
    width_txt = args.value("-w", "--width")
    args.positionals("gout view [-w COLUMNS]")
    width = int(width_txt) if width_txt and width_txt.isdigit() else shutil.get_terminal_size((100, 24)).columns
    tracks = project.tracks()
    master = project.get("master_ms")
    state = (f"{MASTER_WAV} {fmt_ms(int(master))}" if master and project.master.exists()
             else f"{MASTER_WAV} not rendered")
    print(f"proj  {project.get('name')}  {project.rate} Hz  {len(tracks)} track"
          f"{'' if len(tracks) == 1 else 's'}  {state}")
    for label, cells, _, _ in render_timeline(project, width):
        print(f"{label:<{LABEL_W}} {cells}".rstrip())


# --------------------------------------------------------------------------- cheat sheet

CHEAT = """\
CHEAT SHEET          long short        tab flips the pages
TRACKS
 add   a  FILE.. [-a TIME] [-n NAME]  copy into master/
 scan  sc                             new files in master/
 ls    l                              list the tracks
 move  m  TRACK +1s | -500ms | 1:30   later|earlier|place
 trim  t  TRACK -st 2s -et 1:40       soft: file untouched
 trim  t  TRACK -H [-st ..] [-r]      hard: rewrite the file
 trim  t  TRACK -c                    soft trim off
 rm    r  TRACK [-D]                  drop; -D deletes file
MIXER
 mute  mu TRACK [on|off]              mute all off
 solo  s  TRACK [on|off]              solo all off
 gain  g  TRACK -6                    dB, -60 .. +24
 pan   p  TRACK L30 | R30 | C         all start at C
 hp/lp    TRACK 80 [24] | off         cuts, slope dB/oct
 eq    e  TRACK hp80 +3@200 hs8k:-2   peaks gain@hz/q
 eq    e  TRACK on | off | clear      shelves ls100:+2
 eq    e  TRACK voice|warm|air|mud..  presets (eq presets)
 mix   x  [-3] [-v]                   -3 also master.mp3
PROJECT
 undo  u                              not hard trim / rm -D
 view  v  [-w COLS]                   print the timeline
 saveas sa NAME|PATH                  copy the project
 stems sm [DIR] [-A]                  one wav per track
 dump  dp                             the state as json
 import im FILE.json [-s|-t]          apply such a json
 rebuild rb [-f]                      db from master/ + json
 set   se KEY VALUE                   alone: list settings
 stats st                             LUFS / dBTP per track
 new   n  NAME [-R HZ]                48000 Hz by default
 cheat c  sheet on/off  help h        help all: whole page
 quit  q  leave the ui  clear cl      empty the log
 split sp 50 | +5 | -5                left pane width (ui)
 sheet sh (or ctrl-e)                 parameters as a table
MASTER set KEY VALUE
 lufs -14|off  ceiling -1  gain -3    loudness, dBTP, gain
 fadein 1s  fadeout 3s  head 1s  tail 2s
 bits 32f|24|16  mp3 320k|v0  title artist album year
FLAGS  -N --no-mix skip the re-mix    -p DIR the project
       -a --at  -n --name  -H --hard  -c --clear
       -r --reencode  -D --delete  -3 --mp3  -R --rate
       -w --width  -v --verbose
TIMES  2s  500ms  1:30  00:01:30.250  bare number = MINUTES
       trim times count from the track file's start
TRACK  number from ls, or the name (unique prefix ok)
KEYS   ctrl-u  timeline on/off   ctrl-k  sheet on/off
       tab shift-tab  flip sheet (shows it when hidden)
       ctrl-n ctrl-p  sheet line  pgup pgdn      scroll log
       up down  earlier commands  ctrl-l  clear the log
       ctrl-← ctrl-→  move the split  (shift/alt too)
       ctrl-g  eq curve panel on/off  (eq N picks the track)
       ctrl-d  ctrl-c  quit
SHEET  ↑↓ rows, type the new value, ctrl-s apply and stay
       ctrl-x apply and close  esc close  ctrl-w clear cell
       ctrl-shift-s save as (or: saveas NAME at the prompt)
"""


def render_cheat(width: int) -> list[str]:
    lines: list[str] = []
    for line in CHEAT.rstrip("\n").splitlines():
        if len(line) <= width:
            lines.append(line)
        else:
            lines.extend(part.rstrip() for part in textwrap.wrap(
                line, max(20, width), subsequent_indent="           ", replace_whitespace=False))
    return lines


def cmd_cheat(root_hint: Path | None, args: Args) -> None:
    width_txt = args.value("-w", "--width")
    args.positionals("gout cheat [-w COLUMNS]")
    width = int(width_txt) if width_txt and width_txt.isdigit() else shutil.get_terminal_size((100, 24)).columns
    print("\n".join(render_cheat(width)))


# long name -> the short form and the other spellings; every command works under all of them
COMMANDS = {
    "add": ("a",), "scan": ("sc",), "ls": ("l", "list"), "view": ("v",), "move": ("m", "mv"), "trim": ("t",),
    "rm": ("r", "remove", "del"), "mute": ("mu",), "solo": ("s",), "gain": ("g",), "pan": ("p",), "eq": ("e",), "hp": (), "lp": (),
    "mix": ("x", "render", "bounce"), "undo": ("u",), "dump": ("dp",), "rebuild": ("rb",),
    "set": ("se",), "stats": ("st",), "saveas": ("sa", "copy"), "stems": ("sm",), "import": ("im",), "new": ("n",), "cheat": ("c",), "help": ("h", "?"), "ui": ("tui",), "cut": (),
    "quit": ("q", "exit"), "clear": ("cl",), "split": ("sp",), "sheet": ("sh",),
}
ALIASES = {alias: name for name, aliases in COMMANDS.items() for alias in aliases}


# --------------------------------------------------------------------------- terminal ui


# arrow and paging sequences as curses key names, for terminals that send the plain form
ESCAPE_KEYS = {"[A": "KEY_UP", "OA": "KEY_UP", "[B": "KEY_DOWN", "OB": "KEY_DOWN",
               "[C": "KEY_RIGHT", "OC": "KEY_RIGHT", "[D": "KEY_LEFT", "OD": "KEY_LEFT",
               "[5~": "KEY_PPAGE", "[6~": "KEY_NPAGE", "[Z": "KEY_BTAB"}


class Tui:
    """Left: a prompt with a log, like the terminal. Right: the tracks, like a DAW."""

    def __init__(self, project: Project, scr):
        self.project, self.scr = project, scr
        self.log: list[str] = [f"gout {__version__}  {project.root}",
                               "ctrl-u shows/hides the timeline, ctrl-k the cheat sheet, tab flips its pages,"
                               " ctrl-e opens the parameter sheet"]
        self.input = ""
        self.history: list[str] = []
        self.hist_i: int | None = None
        self.mode = "prompt"  # or "sheet": the parameter table
        self.sheet_rows: list[dict] = []
        self.sheet_cur = 0
        self.sheet_top = 0
        self.edits: dict[str, str] = {}
        self.sheet_errors: dict[str, str] = {}
        self.sheet_status = ""
        self.saveas_name: str | None = None  # the "save as:" field in the sheet while it is open
        self.eq_track: int | None = None  # track number whose eq curve the panel shows
        self.show_eq = True
        self.show_timeline = (project.get("ui_timeline") or "on") != "off"
        self.show_cheat = (project.get("ui_cheat") or "on") != "off"
        split = project.get("ui_split") or "40"
        self.split = int(split) if split.isdigit() else 40  # left pane, percent of the width
        self.scroll = 0
        self.cheat_scroll = 0
        self.sheet_h = 10
        self.sheet_len = 0
        self.busy = False
        self.running = True

    # ---- drawing

    def put(self, y: int, x: int, text: str, attr: int = 0, maxw: int | None = None) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        if maxw is not None:
            text = text[:maxw]
        text = text[:w - x]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass  # the bottom-right cell always complains

    def layout(self) -> tuple[int, int, int, int | None, int]:
        h, w = self.scr.getmaxyx()
        if (self.show_timeline or self.show_cheat) and w >= 60:
            left = max(30, min(w - 30, w * max(20, min(80, self.split)) // 100))
            return h, w, left, left + 1, w - left - 1
        return h, w, w, None, 0

    def draw(self) -> None:
        import curses
        scr = self.scr
        scr.erase()
        if self.mode == "sheet":
            self.draw_sheet()
            return
        h, w, left_w, right_x, right_w = self.layout()
        p = self.project
        tracks = p.tracks()
        title = (f" gout {p.get('name')}  {p.rate} Hz  {len(tracks)} track{'' if len(tracks) == 1 else 's'}"
                 f"  autorender {'on' if p.autorender else 'off'}").ljust(left_w)
        if self.scroll:
            tag = " ↑ scrolled, pgdn "
            title = title[:max(0, left_w - len(tag))] + tag
        self.put(0, 0, title, curses.A_REVERSE)

        wrapped: list[str] = []
        for line in self.log:
            wrapped.extend(textwrap.wrap(line, max(10, left_w - 1), subsequent_indent="   ",
                                         drop_whitespace=False, replace_whitespace=False) or [""])
        avail = max(0, h - 2)
        self.scroll = max(0, min(self.scroll, max(0, len(wrapped) - avail)))
        if self.scroll == 0 and len(wrapped) <= avail:  # like a terminal: the prompt sits under the output
            visible, prompt_y = wrapped, 1 + len(wrapped)
        else:  # the screen is full (or scrolled back): the prompt stays on the last row
            end = len(wrapped) - self.scroll
            visible, prompt_y = wrapped[max(0, end - avail):end], h - 1
        for i, line in enumerate(visible):
            attr = curses.A_BOLD if line.startswith("> ") else (curses.A_DIM if line.startswith("error") else 0)
            self.put(1 + i, 0, line, attr, left_w - 1)

        prompt = "… " if self.busy else "> "
        room = max(1, left_w - len(prompt) - 1)
        shown = self.input[-room:]
        self.put(prompt_y, 0, prompt + shown, curses.A_DIM if self.busy else curses.A_BOLD)

        if right_x is not None:
            for y in range(h):
                self.put(y, right_x - 1, "│", curses.A_DIM)
            top = 0
            if self.show_timeline:
                self.put(0, right_x, " timeline".ljust(right_w), curses.A_REVERSE)
                rows = render_timeline(p, right_w, styled=True)
                room = max(3, h - 2 - 6) if self.show_cheat else max(3, h - 1)  # sheet keeps six lines
                if self.show_eq and self.eq_track is not None:
                    room = max(3, room - (10 if h >= 32 else 8))
                if len(rows) > room:
                    heads = [r for r in rows if r[2] in ("axis", "ruler")]
                    tail = [r for r in rows if r[2] in ("master", "note") and r not in heads]
                    tracks = [r for r in rows if r[2] == "track"]
                    keep = max(1, room - len(heads) - len(tail) - 1)
                    rows = heads + tracks[:keep] + [("", f"+{len(tracks) - keep} more tracks — ls", "note", "")] + tail
                for y, (label, cells, kind, classes) in enumerate(rows, 1):
                    if y >= h:
                        break
                    self.put(y, right_x, label, curses.A_DIM if kind in ("axis", "ruler", "note") else 0)
                    self.draw_cells(y, right_x + LABEL_W + 1, cells, kind, classes)
                top = 1 + len(rows)

        if right_x is not None and self.show_eq and self.eq_track is not None:
            track = next((t for t in tracks if t["n"] == self.eq_track), None)
            if track is None:
                self.eq_track = None
            else:
                height = 8 if h >= 32 else 6
                rows = render_eq(p, track, right_w - 1, height, self.spectrum_for(track))
                self.put(top, right_x, (" " + rows[0][0] + "   ctrl-g hides").ljust(right_w), curses.A_REVERSE)
                for i, (text, classes, kind) in enumerate(rows[1:], 1):
                    if top + i >= h:
                        break
                    self.put(top + i, right_x + 1, text[:EQ_GUTTER], curses.A_DIM)
                    self.draw_cells(top + i, right_x + 1 + EQ_GUTTER, text[EQ_GUTTER:], kind, classes[EQ_GUTTER:])
                top += len(rows)

        if right_x is not None and self.show_cheat:
            sheet = render_cheat(right_w - 1)
            self.sheet_h = max(1, h - top - 1)
            self.sheet_len = len(sheet)
            self.cheat_scroll = max(0, min(self.cheat_scroll, max(0, len(sheet) - self.sheet_h)))
            pages = max(1, math.ceil(len(sheet) / self.sheet_h))
            page = min(pages, math.ceil((self.cheat_scroll + self.sheet_h) / self.sheet_h))
            self.put(top, right_x, f" cheat sheet  {page}/{pages}  tab".ljust(right_w), curses.A_REVERSE)
            for i, line in enumerate(sheet[self.cheat_scroll:self.cheat_scroll + self.sheet_h]):
                header = line[:1].isupper() and not line.startswith(" ")
                self.put(top + 1 + i, right_x + 1, line, curses.A_BOLD if header else 0, right_w - 1)
        try:
            scr.move(prompt_y, min(len(prompt) + len(shown), w - 1))
        except curses.error:
            pass
        scr.refresh()

    def spectrum_for(self, track: dict) -> bytes | None:
        return self.project.spectrum(track["file"], self.project.tracks_dir / track["file"]) or None

    def draw_cells(self, y: int, x: int, cells: str, kind: str, classes: str = "") -> None:
        import curses
        if kind in ("note", "axis", "ruler"):
            self.put(y, x, cells, curses.A_DIM)
            return
        attrs = {"a": curses.A_BOLD, "s": curses.A_DIM, "t": curses.A_DIM, "z": curses.A_DIM, "m": curses.A_BOLD,
                 "x": curses.A_DIM}
        classes = classes.ljust(len(cells))
        i = 0
        while i < len(cells):
            j = i
            while j < len(cells) and classes[j] == classes[i]:
                j += 1
            self.put(y, x + i, cells[i:j], attrs.get(classes[i], 0))
            i = j

    # ---- input

    def loop(self) -> None:
        import curses
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        while self.running:
            self.draw()
            try:
                key = self.scr.get_wch()
            except KeyboardInterrupt:
                break
            except curses.error:
                continue
            self.handle(key)

    def handle(self, key) -> None:
        import curses
        if key == curses.KEY_RESIZE:
            return
        if self.mode == "sheet":
            self.handle_sheet(key)
            return
        if key == "\x05":  # ctrl-e
            self.sheet_open()
        elif key == "\x07":  # ctrl-g: the eq curve panel
            self.toggle_eq()
        elif key == "\x15":  # ctrl-u
            self.toggle("timeline")
        elif key == "\x0b":  # ctrl-k
            self.toggle("cheat")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.submit()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.input = self.input[:-1]
        elif key == "\x04":  # ctrl-d on an empty line leaves
            if not self.input:
                self.running = False
        elif key == "\x0c":  # ctrl-l
            self.log.clear()
        elif key == "\x17":  # ctrl-w
            self.input = self.input.rstrip()
            self.input = self.input[:self.input.rfind(" ") + 1] if " " in self.input else ""
        elif key == "\x1b":
            seq = self.read_escape()
            arrow = re.fullmatch(r"\[1;([235])([CD])", seq)  # shift/alt/ctrl + right/left
            if arrow:
                self.resize(5 if arrow.group(2) == "C" else -5)
            elif seq in ESCAPE_KEYS:  # a terminal that did not follow curses into application mode
                self.handle(getattr(curses, ESCAPE_KEYS[seq]))
            elif not seq:
                self.input = ""
        elif key in (curses.KEY_SLEFT, curses.KEY_SRIGHT):
            self.resize(5 if key == curses.KEY_SRIGHT else -5)
        elif isinstance(key, int) and self.keyname(key)[:4] in (b"kLFT", b"kRIT"):  # ctrl/alt + arrows
            self.resize(5 if self.keyname(key)[:4] == b"kRIT" else -5)
        elif key == curses.KEY_UP:
            if self.history:
                self.hist_i = len(self.history) - 1 if self.hist_i is None else max(0, self.hist_i - 1)
                self.input = self.history[self.hist_i]
        elif key == curses.KEY_DOWN:
            if self.hist_i is not None:
                self.hist_i += 1
                if self.hist_i >= len(self.history):
                    self.hist_i, self.input = None, ""
                else:
                    self.input = self.history[self.hist_i]
        elif key == curses.KEY_PPAGE:
            self.scroll += 10
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
        elif key in ("\t", curses.KEY_BTAB, "\x0e", "\x10"):  # tab, shift-tab, ctrl-n, ctrl-p
            if not self.show_cheat:
                self.toggle("cheat")
                return
            last = max(0, self.sheet_len - self.sheet_h)
            step = self.sheet_h if key in ("\t", curses.KEY_BTAB) else 1
            if key in ("\t", "\x0e"):
                at_end = self.cheat_scroll >= last
                self.cheat_scroll = 0 if at_end and key == "\t" else min(last, self.cheat_scroll + step)
            else:
                at_top = self.cheat_scroll <= 0
                self.cheat_scroll = last if at_top and key == curses.KEY_BTAB else max(0, self.cheat_scroll - step)
        elif isinstance(key, str) and key.isprintable():
            self.input += key
            self.scroll = 0

    # ---- parameter sheet: name | value | new value, ctrl-s applies

    def sheet_build(self) -> list[dict]:
        """Every parameter in the database as a row; edits map to ordinary commands."""
        p = self.project
        rows: list[dict] = []

        def head(text: str) -> None:
            rows.append({"id": None, "head": True, "name": text, "value": "", "hint": "", "cmd": None})

        def row(rid: str, name: str, value: str, cmd, hint: str = "") -> None:
            rows.append({"id": rid, "head": False, "name": name, "value": value, "hint": hint, "cmd": cmd})

        hints = {
            "rate": "Hz", "autorender": "on | off",
            "lufs": "-14 | -16 | -23 | off", "ceiling": "dBTP", "gain": "dB",
            "fadein": "500ms", "fadeout": "3s", "head": "500ms", "tail": "2s",
            "bits": "32f | 24 | 16", "mp3": "320k | 192k | v0",
        }
        head(f"project  {p.get('name')}")
        row("set:rate", "rate", str(p.rate), lambda v: ["set", "rate", v], hints["rate"])
        row("set:autorender", "autorender", "on" if p.autorender else "off",
            lambda v: ["set", "autorender", v], hints["autorender"])
        for key in MASTER_DEFAULTS:
            value = setting(p, key)
            if key in ("fadein", "fadeout", "head", "tail") and value != "0":
                value = fmt_ms(int(value))
            row(f"set:{key}", key, value, (lambda k: lambda v: ["set", k, v])(key), hints.get(key, "text"))
        for t in p.tracks():
            n = str(t["n"])
            a, b = audible(t)
            start, _ = timeline(t)
            head(f"track {n}  {t['name']}  {t['kind']}  {fmt_ms(t['length_ms'])}")
            row(f"t{n}:at", "at", fmt_ms(start), lambda v, n=n: ["move", n, "=" + v], "timeline start: 1:30, 90s, -2s")
            row(f"t{n}:in", "in", fmt_ms(a), lambda v, n=n: ["trim", n, "-st", v], "soft trim in, file time")
            row(f"t{n}:out", "out", fmt_ms(b), lambda v, n=n: ["trim", n, "-et", v], "soft trim out, file time")
            row(f"t{n}:gain", "gain", f"{t['gain_db']:g}", lambda v, n=n: ["gain", n, v], "dB")
            row(f"t{n}:pan", "pan", fmt_pan(t["pan"]), lambda v, n=n: ["pan", n, v], "L30 | C | R30")
            row(f"t{n}:mute", "mute", "on" if t["mute"] else "off", lambda v, n=n: ["mute", n, v], "on | off")
            row(f"t{n}:solo", "solo", "on" if t["solo"] else "off", lambda v, n=n: ["solo", n, v], "on | off")
            row(f"t{n}:eq", "eq", (t["eq"] or "flat") + ("" if t["eq_on"] else " (off)"),
                lambda v, n=n: ["eq", n, *v.split()], "hp80 +3@200 hs8k:-2 | voice | off | clear")
        return rows

    def sheet_open(self) -> None:
        self.mode = "sheet"
        self.sheet_rows = self.sheet_build()
        if not self.sheet_rows[self.sheet_cur]["id"] if self.sheet_cur < len(self.sheet_rows) else True:
            self.sheet_cur = next((i for i, r in enumerate(self.sheet_rows) if r["id"]), 0)
        pending = len(self.edits)
        self.sheet_status = f"{pending} unapplied change{'s' if pending != 1 else ''} kept from last time" if pending else ""

    def sheet_close(self) -> None:
        self.mode = "prompt"
        self.saveas_name = None

    def save_as(self, name: str) -> None:
        """Copy the project and carry on in the copy, the way a DAW's Save As does."""
        try:
            copy = save_as(self.project, name)
        except GoutError as exc:
            self.log.append(f"error: {exc}")
            self.sheet_status = str(exc)
            return
        self.project = copy
        self.edits.clear()
        self.sheet_errors.clear()
        self.log.append(f"saved as {copy.root} — you are now working in the copy; the original is untouched")
        self.sheet_status = f"now in {copy.root.name}"

    def sheet_move(self, delta: int) -> None:
        rows = self.sheet_rows
        i = self.sheet_cur
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            j = i + step
            while 0 <= j < len(rows) and not rows[j]["id"]:
                j += step
            if not 0 <= j < len(rows):
                break
            i = j
        self.sheet_cur = i

    def draw_sheet(self) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        rows = self.sheet_rows = self.sheet_build()
        if not (0 <= self.sheet_cur < len(rows)) or not rows[self.sheet_cur]["id"]:
            self.sheet_cur = next((i for i, r in enumerate(rows) if r["id"]), 0)
        pending = len(self.edits)
        title = (f" sheet  {pending} change{'s' if pending != 1 else ''}   ctrl-s apply   ctrl-x apply and close"
                 f"   esc close   ↑ ↓ rows")
        self.put(0, 0, title.ljust(w), curses.A_REVERSE)
        name_w = 12
        val_w = max(14, min(28, (w - name_w - 6) // 3))
        new_x = 2 + name_w + 1 + val_w + 1
        self.put(1, 0, f"  {'name':<{name_w}} {'value':<{val_w}} new value", curses.A_DIM)
        avail = max(1, h - 3)
        if self.sheet_cur < self.sheet_top:
            self.sheet_top = self.sheet_cur
        if self.sheet_cur >= self.sheet_top + avail:
            self.sheet_top = self.sheet_cur - avail + 1
        self.sheet_top = max(0, min(self.sheet_top, max(0, len(rows) - avail)))
        cursor = (h - 1, 0)
        for i, r in enumerate(rows[self.sheet_top:self.sheet_top + avail]):
            y = 2 + i
            if r["head"]:
                self.put(y, 0, r["name"], curses.A_BOLD)
                continue
            current = self.sheet_top + i == self.sheet_cur
            edit = self.edits.get(r["id"])
            mark = "!" if r["id"] in self.sheet_errors else ("*" if edit is not None else " ")
            self.put(y, 0, f"{mark} {r['name']:<{name_w}} {r['value'][:val_w]:<{val_w}} ".ljust(new_x),
                     curses.A_REVERSE if current else 0)
            if edit:
                self.put(y, new_x, edit, curses.A_BOLD | (curses.A_REVERSE if current else 0))
            else:
                self.put(y, new_x, r["hint"], curses.A_DIM)
            if current:
                cursor = (y, min(w - 1, new_x + len(edit or "")))
        if self.saveas_name is not None:
            status = f"save as: {self.saveas_name}   (enter copies the whole project there, esc cancels)"
            cursor = (h - 1, min(w - 1, 9 + len(self.saveas_name)))
            self.put(h - 1, 0, status[:w - 1], curses.A_BOLD)
        else:
            status = self.sheet_errors.get(rows[self.sheet_cur]["id"] or "", "") or self.sheet_status \
                or "type a new value on the highlighted row; enter or ↓ for the next"
            self.put(h - 1, 0, status[:w - 1], curses.A_DIM if not self.sheet_errors else 0)
        try:
            self.scr.move(*cursor)
        except curses.error:
            pass
        self.scr.refresh()

    def sheet_apply(self, close: bool) -> None:
        rows = self.sheet_rows
        done = failed = 0
        buf = io.StringIO()
        for r in rows:
            if not r["id"] or r["id"] not in self.edits:
                continue
            argv = r["cmd"](self.edits[r["id"]]) + ["-N"]
            self.log.append("> " + " ".join(argv))
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(argv, self.project)
                self.edits.pop(r["id"], None)
                self.sheet_errors.pop(r["id"], None)
                done += 1
            except GoutError as exc:
                self.sheet_errors[r["id"]] = str(exc).splitlines()[0]
                buf.write(f"error: {exc}\n")
                failed += 1
        self.log.extend(buf.getvalue().rstrip("\n").splitlines())
        if done and self.project.autorender:
            self.sheet_status = "rendering…"
            self.draw()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(["mix"], self.project)
            except GoutError as exc:
                buf.write(f"error: {exc}\n")
            self.log.extend(buf.getvalue().rstrip("\n").splitlines())
        self.sheet_status = f"applied {done}" + (f", {failed} failed — see the ! rows" if failed else "")
        if close and not failed:
            self.sheet_close()

    def handle_sheet(self, key) -> None:
        import curses
        rows = self.sheet_rows
        rid = rows[self.sheet_cur]["id"] if rows and 0 <= self.sheet_cur < len(rows) else None
        if self.saveas_name is not None:  # typing the name for save as
            if key in ("\n", "\r", curses.KEY_ENTER):
                name, self.saveas_name = self.saveas_name.strip(), None
                if name:
                    if self.edits:
                        self.sheet_apply(close=False)
                    if not self.sheet_errors:
                        self.save_as(name)
            elif key == "\x1b" and not self.read_escape():
                self.saveas_name = None
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                self.saveas_name = self.saveas_name[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.saveas_name += key
            return
        if key == "\x1b":
            seq = self.read_escape()
            if re.fullmatch(r"\[(83|115);6u", seq):  # ctrl-shift-s, in terminals that can send it: save as
                self.saveas_name = ""
                self.sheet_status = ""
            elif seq in ESCAPE_KEYS:
                self.handle_sheet(getattr(curses, ESCAPE_KEYS[seq]))
            elif not seq:
                self.sheet_close()
        elif key == "\x13":  # ctrl-s
            self.sheet_apply(close=False)
        elif key == "\x18":  # ctrl-x
            self.sheet_apply(close=True)
        elif key in ("\x05", "\x11"):  # ctrl-e again, ctrl-q
            self.sheet_close()
        elif key == "\x03":
            self.running = False
        elif key == curses.KEY_UP or key == curses.KEY_BTAB:
            self.sheet_move(-1)
        elif key in (curses.KEY_DOWN, "\n", "\r", curses.KEY_ENTER, "\t"):
            self.sheet_move(1)
        elif key == curses.KEY_PPAGE:
            self.sheet_move(-10)
        elif key == curses.KEY_NPAGE:
            self.sheet_move(10)
        elif rid and key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            edit = self.edits.get(rid, "")[:-1]
            if edit:
                self.edits[rid] = edit
            else:
                self.edits.pop(rid, None)
            self.sheet_errors.pop(rid, None)
        elif rid and key == "\x17":  # ctrl-w clears the cell
            self.edits.pop(rid, None)
            self.sheet_errors.pop(rid, None)
        elif rid and isinstance(key, str) and key.isprintable():
            self.edits[rid] = self.edits.get(rid, "") + key
            self.sheet_errors.pop(rid, None)

    @staticmethod
    def keyname(key: int) -> bytes:
        import curses
        try:
            return curses.keyname(key)
        except (curses.error, ValueError):
            return b""

    def read_escape(self) -> str:
        """The rest of an escape sequence curses did not recognise, or '' for a bare Esc."""
        import curses
        seq = ""
        self.scr.nodelay(True)
        try:
            for _ in range(8):
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    break
                if not isinstance(ch, str):
                    break
                seq += ch
                if ch.isalpha() or ch == "~":
                    break
        finally:
            self.scr.nodelay(False)
        return seq

    def resize(self, delta: int, absolute: int | None = None) -> None:
        self.split = max(20, min(80, self.split + delta if absolute is None else absolute))
        self.project.set("ui_split", str(self.split))
        if not (self.show_timeline or self.show_cheat):
            self.toggle("timeline")

    def toggle_eq(self) -> None:
        if self.eq_track is None:
            tracks = self.project.tracks()
            if not tracks:
                self.log.append("no tracks yet, nothing to show an eq for")
                return
            self.eq_track, self.show_eq = tracks[0]["n"], True
        else:
            self.show_eq = not self.show_eq

    def toggle(self, what: str) -> None:
        """Show or hide one section of the right panel; remembered per project."""
        if what == "timeline":
            self.show_timeline = not self.show_timeline
            self.project.set("ui_timeline", "on" if self.show_timeline else "off")
        else:
            self.show_cheat = not self.show_cheat
            self.project.set("ui_cheat", "on" if self.show_cheat else "off")

    def submit(self) -> None:
        line = self.input.strip()
        self.input, self.scroll, self.hist_i = "", 0, None
        if not line:
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
        self.log.append("> " + line)
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            self.log.append(f"error: {exc}")
            return
        head = ALIASES.get(argv[0], argv[0])
        if head in ("q", "quit", "exit"):
            self.running = False
        elif head in ("view", "timeline"):
            self.toggle("timeline")
        elif head == "cheat":
            self.toggle("cheat")
        elif head == "sheet":
            self.sheet_open()
        elif head == "eq" and len(argv) == 1:
            self.toggle_eq()
        elif head == "clear":
            self.log.clear()
        elif head == "split":
            arg = argv[1] if len(argv) > 1 else ""
            if re.fullmatch(r"[+-]\d+", arg):
                self.resize(int(arg))
            elif arg.isdigit():
                self.resize(0, int(arg))
            elif arg:
                self.log.append("split takes a percentage (split 50) or a step (split +5, split -5)")
            self.log.append(f"split  left pane {self.split}% of the width  (ctrl-← ctrl-→ move it)")
        elif head in ("help", "-h", "--help"):
            full = argv[1:] == ["all"]
            self.log.extend(HELP.rstrip().splitlines() if full else render_cheat(max(40, self.layout()[2] - 2)))
        elif head == "saveas":
            if len(argv) != 2:
                self.log.append("saveas NAME  (a bare name goes next to this project; a path goes where it says)")
            else:
                self.save_as(argv[1])
        elif head in ("ui", "tui", "rebuild", "new"):
            self.log.append(f"{head}: run that from the shell")
        else:
            self.busy = True
            self.draw()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    run(argv, self.project)
            except GoutError as exc:
                buf.write(f"error: {exc}\n")
            except Exception as exc:  # keep the UI alive whatever happens
                buf.write(f"error: {type(exc).__name__}: {exc}\n")
            finally:
                self.busy = False
            self.log.extend(buf.getvalue().rstrip("\n").splitlines())
            if head in ("eq", "hp", "lp") and len(argv) > 1:
                try:  # the panel follows the track you are working on
                    self.eq_track, self.show_eq = self.project.track(argv[1])["n"], True
                except GoutError:
                    pass
        del self.log[:-2000]


def run_tui(project: Project) -> None:
    import curses
    import locale
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # a bare Esc should not wait a second

    def start(scr) -> None:
        try:  # ctrl-s is XOFF to the terminal driver unless IXON is off; endwin restores the old mode
            import termios
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        Tui(project, scr).loop()

    curses.wrapper(start)


def cmd_ui(project: Project, args: Args) -> None:
    args.positionals("gout ui")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die("the ui needs a terminal")
    run_tui(project)


# --------------------------------------------------------------------------- cut (the 1.x command)

HELP = f"""\
gout {__version__} — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

Every command has a long and a short name (gout add / gout a). gout cheat prints the sheet.

PROJECT
  gout new   n  NAME [-R HZ]      create NAME/ with {TRACK_DIR}/ and {DB_NAME} (default {DEFAULT_RATE} Hz)
  gout                            inside a project: open the terminal ui (prompt left; timeline and
                                  cheat sheet right, ctrl-u / ctrl-k hide each; ctrl-e opens the
                                  parameter sheet: every setting and track parameter as name, value
                                  and new value, ctrl-s applies); elsewhere: this page
  gout view  v                    print the timeline once: one row per track, each column a
                                  block ▁▂▃▄▅▆▇█ as tall as the peak there (6 dB per step)
  gout cheat c                    print the cheat sheet
  gout ls    l                    list the tracks and the state of {MASTER_WAV}
  gout mix   x  [-3] [-v]         render {MASTER_WAV} (32-bit float stereo); -3 also writes {MASTER_MP3}
  gout undo  u                    undo the last change (not a hard trim or rm -D)
  gout saveas sa NAME | PATH      copy the whole project (files included) next to this one, or to PATH
  gout stems sm [DIR] [-A]        one wav per track, trimmed, placed, gained and panned as in the mix,
                                  all the same length from 0:00, into {STEMS_DIR}/ (-A: only what the mix hears)
  gout dump  dp                   print the project state as JSON: the same document gout keeps in
                                  {SIDECAR} next to the database, rewritten after every change
  gout import im FILE.json [-s|-t]  apply such a document: settings, and tracks matched by file name
                                  (-s settings only, e.g. a master template; -t tracks only)
  gout rebuild rb [-f]            recreate {DB_NAME} from the files in {TRACK_DIR}/, then restore positions,
                                  trims and settings from {SIDECAR} when it is there
  gout set   se KEY VALUE         settings; gout set alone lists them:  autorender on|off,  rate HZ
  gout stats st                   integrated LUFS, LRA and true peak per track file, and for {MASTER_WAV}

MASTER   (gout set KEY VALUE)
  lufs -14 | off        loudness target. Two passes of ffmpeg's loudnorm: a plain gain change
                        whenever the ceiling allows, otherwise dynamic, and the mix line says which.
                        -14 streaming (Spotify, YouTube), -16 Apple Music and podcasts, -23 broadcast
  ceiling -1            true-peak ceiling in dBTP for that step (default -1)
  gain -3               master gain in dB before the loudness step
  fadein 500ms          fades on the sum;  fadeout 3s
  head 500ms  tail 2s   silence padded before and after
  bits 32f | 24 | 16    {MASTER_WAV} format (16 is dithered);  mp3 320k | 192k | v0  bounce quality
  title artist album year comment   tags written into {MASTER_WAV} and {MASTER_MP3}
  Every mix line reports the result:  mix   master.wav  03:12.500  -14.0 LUFS  LRA 6.2  peak -1.0 dBTP

TRACKS   (TRACK is the number shown by ls, or the track name)
  gout add   a  FILE... [-n NAME] [-a TIME]  copy wav/mp3 into {TRACK_DIR}/ (other formats become wav)
  gout scan  sc                              register wav/mp3 you copied into {TRACK_DIR}/ yourself,
                                             at 0; reports tracks whose file has gone missing
  gout move  m  TRACK +TIME | -TIME | TIME   nudge later, nudge earlier, or place at a time
  gout trim  t  TRACK [-st T] [-et T|-el T]  soft trim: in/out points, the file is untouched
  gout trim  t  TRACK -c                     soft trim off again (--clear)
  gout trim  t  TRACK -H [-st ..] [-et ..]   hard trim: rewrite the file, bakes the soft trim (--hard)
  gout rm    r  TRACK [-D]                   drop a track; -D also deletes its file (--delete)
  gout mute  mu TRACK [on|off]               toggle mute        (mute all off)
  gout solo  s  TRACK [on|off]               toggle solo        (solo all off)
  gout gain  g  TRACK DB                     gain 2 -6
  gout pan   p  TRACK C | L30 | R30          balance; every track starts centred, 50/50
  gout hp       TRACK HZ [SLOPE] | off       high-pass cut, e.g. hp 3 80, hp 3 80 24 (dB per octave)
  gout lp       TRACK HZ [SLOPE] | off       low-pass cut, e.g. lp 3 12k
  gout eq    e  TRACK BANDS...               the whole eq in one line, before the fader:
                                             {EQ_SYNTAX}
  gout eq    e  TRACK PRESET [BANDS...]      a named start: {' '.join(EQ_PRESETS)}
  gout eq    e  TRACK on | off | clear       bypass, bring back, or remove;  eq presets lists them
  gout eq    e  TRACK                        show the bands and draw the curve, 20 Hz to 20 kHz; in
                                             the ui the curve panel follows the track you eq (ctrl-g)
  -N (--no-mix) on any of these skips the automatic re-mix; -p DIR before a command picks
  the project. Long flags: --at --name --hard --clear --reencode --delete --mp3 --rate --width

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

SOFT AND HARD TRIM
  Trim times count from the start of the track's own file, not from the timeline.
  A soft trim only stores in/out points; the mix applies them sample-exactly and
  you can change them as often as you like. A hard trim rewrites the file in {TRACK_DIR}/
  so the material gets lighter; with no times it bakes the current soft trim. The
  sound stays where it was on the timeline. mp3 hard trims copy whole frames (26 ms
  grid, nothing re-encoded); -r re-encodes for the exact millisecond. wav is exact.

HOW THE MIX WORKS
  Every track is decoded, trimmed, delayed to its position, panned to stereo and summed
  with ffmpeg's amix (normalize off, so adding a track never turns the others down).
  {MASTER_WAV} is 32-bit float, so a hot sum cannot clip there; the peak is reported.

HOW -fs WORKS
  The length is estimated from the bitrate, cut, then measured and retried until the
  file lands between 97% and 100% of the limit — never over it.

NOTES
  Needs ffmpeg and ffprobe on PATH. gout cut on an mp3 copies the stream, which starts
  up to ~0.1 s late (ffmpeg's seek); -r re-encodes for the exact millisecond. wav is exact.
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
    "add": cmd_add, "scan": cmd_scan, "ls": cmd_ls, "move": cmd_move, "trim": cmd_trim, "rm": cmd_rm,
    "mute": cmd_mute, "solo": cmd_solo, "gain": cmd_gain, "pan": cmd_pan,
    "eq": cmd_eq, "hp": cmd_hp, "lp": cmd_lp,
    "set": cmd_set, "stats": cmd_stats, "mix": cmd_mix, "undo": cmd_undo, "dump": cmd_dump,
    "saveas": cmd_saveas, "stems": cmd_stems, "import": cmd_import,
    "view": cmd_view, "ui": cmd_ui,
}
FREE_COMMANDS = {"new": cmd_new, "rebuild": cmd_rebuild, "cheat": cmd_cheat}


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

    if project is not None and root_hint is None:
        root_hint = project.root

    if not argv:
        found = project or Project.find(root_hint)
        if found is None:
            print(HELP, end="")
            return 0
        need_tools()
        if sys.stdin.isatty() and sys.stdout.isatty():
            run_tui(found)
        else:
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
    if head in ("quit", "clear", "split", "sheet") and project is None:
        die(f"{head} only means something inside the ui (gout, in a project)")

    need_tools()
    if head in FREE_COMMANDS:
        FREE_COMMANDS[head](root_hint, Args(rest))
        return 0
    if head in PROJECT_COMMANDS:
        found = project or Project.find(root_hint)
        if found is None:
            die(f"not inside a gout project (no {DB_NAME} here or above) — gout new NAME")
        PROJECT_COMMANDS[head](found, Args(rest))
        found.sync_json()
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
