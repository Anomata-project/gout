"""Music analysis for screens: how loud the lows, mids and highs are, 40 times a second.

ffmpeg splits a mono 16 kHz decode into three bands (below 150 Hz, 300 Hz to 2.5 kHz, above
5 kHz) and hands them over as one three-channel stream; Python takes the RMS of every 25 ms.
Results are cached per file under .gout/analysis/, keyed by the file's size and modification
time, so a song is listened to once.

The features follow project time. When master.wav matches the project they come from it (its
head padding skipped); otherwise from the track files placed as the timeline places them,
gain, mute and solo applied, effects left out, so a screen still moves with an unrendered mix.

The database is read in `plan` (on the ui's thread); `compute` touches only files, so it can run
in a background thread.
"""
from __future__ import annotations

import hashlib
import math
import subprocess
from array import array
from pathlib import Path

from .model import audible, is_heard
from .settings import setting

RATE = 16000
HOP = 400                     # samples per frame: 25 ms
FRAME_MS = HOP * 1000 // RATE
BANDS = ("low", "mid", "high")
VERSION = 1                   # bump when the analysis changes, so caches are redone
RANGE_DB = 30.0               # a band reads 0 this far below its loud moments, 1 at them
CACHE_DIR = ".gout/analysis"

# join maps mono inputs by channel name unless told: without the map the first input lands on
# FC, the third channel (measured; see notes/ffmpeg-quirks.md)
GRAPH = ("[0:a]aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000,asplit=3[a][b][c];"
         "[a]lowpass=f=150,lowpass=f=150[l];"
         "[b]highpass=f=300,lowpass=f=2500[m];"
         "[c]highpass=f=5000,highpass=f=5000[h];"
         "[l][m][h]join=inputs=3:channel_layout=3.0:map=0.0-FL|1.0-FR|2.0-FC[o]")


def _sumsq(values: array) -> float:
    try:
        return math.sumprod(values, values)  # Python 3.12: in C
    except AttributeError:
        return float(sum(v * v for v in values))


def band_rms(path: Path, cache_dir: Path | None = None) -> dict[str, array]:
    """RMS of each band per 25 ms of a file, in sample units (0 .. 32767). Empty when ffmpeg
    cannot read it."""
    cached = None
    if cache_dir is not None:
        st = path.stat()
        key = hashlib.sha1(f"v{VERSION}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]
        cached = cache_dir / f"{key}.bands"
        if cached.exists():
            data = array("f")
            data.frombytes(cached.read_bytes())
            n = len(data) // 3
            return {name: data[i * n:(i + 1) * n] for i, name in enumerate(BANDS)}
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                             "-filter_complex", GRAPH, "-map", "[o]", "-f", "s16le", "-"],
                            capture_output=True)
    if result.returncode != 0:
        return {name: array("f") for name in BANDS}
    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 6 * 6])
    out = {}
    for i, name in enumerate(BANDS):
        channel = samples[i::3]
        out[name] = array("f", (math.sqrt(_sumsq(channel[j:j + HOP]) / HOP)
                                for j in range(0, len(channel) - HOP + 1, HOP)))
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        blob = array("f")
        for name in BANDS:
            blob.extend(out[name])
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(blob.tobytes())
        tmp.replace(cached)
    return out


class Features:
    """The bands over project time, each 0 .. 1, plus `level` (all of it) and `onset` (how
    suddenly the lows and mids rose). Prefix sums make averages and running totals cheap."""

    NAMES = BANDS + ("level", "onset")

    def __init__(self, raw: dict[str, array]):
        n = max((len(v) for v in raw.values()), default=0)
        energy = [0.0] * n
        self.values: dict[str, list[float]] = {}
        for name in BANDS:
            band = list(raw.get(name, [])) + [0.0] * (n - len(raw.get(name, [])))
            for i, v in enumerate(band):
                energy[i] += v * v
            self.values[name] = scaled(band)
        self.values["level"] = scaled([math.sqrt(e) for e in energy])
        body = [max(a, b) for a, b in zip(self.values["low"], self.values["mid"])]
        onset = []
        for i, v in enumerate(body):
            before = body[max(0, i - 6):i]
            rise = v - (sum(before) / len(before) if before else v)
            onset.append(min(1.0, max(0.0, rise * 3)))
        self.values["onset"] = onset
        self.sums = {}
        for name, vals in self.values.items():
            total, sums = 0.0, [0.0]
            for v in vals:
                total += v
                sums.append(total)
            self.sums[name] = sums
        self.frames = n

    @property
    def length_ms(self) -> int:
        return self.frames * FRAME_MS

    def at(self, name: str, ms: float) -> float:
        i = int(ms // FRAME_MS)
        vals = self.values[name]
        return vals[i] if 0 <= i < len(vals) else 0.0

    def total(self, name: str, ms: float) -> float:
        """The band summed from the start to ms, in seconds at full level: it only grows, so
        something driven by it moves further when the band is busy and stands still in silence."""
        sums = self.sums[name]
        i = min(len(sums) - 1, max(0, int(ms // FRAME_MS)))
        return sums[i] * FRAME_MS / 1000

    def mean(self, name: str, ms: float, window_ms: float) -> float:
        """The average over the window that ends at ms: a band without the flicker."""
        window_ms = max(FRAME_MS, window_ms)
        sums = self.sums[name]
        hi = min(len(sums) - 1, max(0, int(ms // FRAME_MS) + 1))
        lo = min(len(sums) - 1, max(0, int((ms - window_ms) // FRAME_MS) + 1))
        frames = max(1, round(window_ms / FRAME_MS))
        return (sums[hi] - sums[lo]) / frames


def scaled(values: list[float]) -> list[float]:
    """0 .. 1 on a dB scale: 1 at the band's loud moments (its 98th percentile), 0 at RANGE_DB below."""
    audible_values = sorted(v for v in values if v > 1.0)
    if not audible_values:
        return [0.0] * len(values)
    ref = audible_values[min(len(audible_values) - 1, int(len(audible_values) * 0.98))]
    out = []
    for v in values:
        if v <= 1e-3:
            out.append(0.0)
            continue
        out.append(min(1.0, max(0.0, 1 + 20 * math.log10(v / ref) / RANGE_DB)))
    return out


def plan(project) -> dict:
    """What to listen to, read from the database: master.wav when current, else the tracks."""
    cache = project.root / CACHE_DIR
    if project.master_is_current():
        return {"cache": cache, "master": project.master, "head_ms": int(setting(project, "head"))}
    tracks = project.tracks()
    any_solo = any(t["solo"] for t in tracks)
    placed = []
    for t in tracks:
        if not is_heard(t, any_solo):
            continue
        a, b = audible(t)
        placed.append({"path": project.tracks_dir / t["file"], "offset_ms": t["offset_ms"], "in_ms": a,
                       "out_ms": b, "gain": 10 ** (t["gain_db"] / 20)})
    return {"cache": cache, "tracks": placed}


def compute(p: dict) -> Features:
    """The features for a plan. Files only: safe off the ui's thread."""
    if "master" in p:
        raw = band_rms(p["master"], p["cache"])
        skip = p["head_ms"] // FRAME_MS
        return Features({name: raw[name][skip:] for name in BANDS})
    end = max((t["offset_ms"] + t["out_ms"] for t in p["tracks"]), default=0)
    n = end // FRAME_MS + 1
    energy = {name: [0.0] * n for name in BANDS}
    for t in p["tracks"]:
        raw = band_rms(t["path"], p["cache"])
        first, last = (t["offset_ms"] + t["in_ms"]) // FRAME_MS, (t["offset_ms"] + t["out_ms"]) // FRAME_MS
        shift = t["offset_ms"] // FRAME_MS
        for name in BANDS:
            src, dst, g2 = raw[name], energy[name], t["gain"] ** 2
            for f in range(max(0, first), min(n, last)):
                i = f - shift
                if 0 <= i < len(src):
                    dst[f] += src[i] * src[i] * g2
    return Features({name: array("f", (math.sqrt(e) for e in energy[name])) for name in BANDS})


def project_features(project) -> Features:
    return compute(plan(project))
