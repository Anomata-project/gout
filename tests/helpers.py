"""Shared fixtures for the gout test suite: a runner, generated audio, and measurements.

Everything is black-box where possible: tests run the real command in a temporary
directory and measure the audio it writes with ffmpeg.
"""
from __future__ import annotations

import array
import importlib
import json
import math
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RATE = 48000

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def gout_cmd() -> list[str]:
    if os.environ.get("GOUT_TEST_COMMAND"):  # a built gout: CI runs the tests on the installers' program
        return [os.environ["GOUT_TEST_COMMAND"]]
    launcher = REPO / "bin" / "gout"
    return [sys.executable, str(launcher if launcher.exists() else REPO / "gout.py")]


def gout_attr(module: str, name: str):
    """A name from the code under test: gout.<module> in the package, or the old single file."""
    import gout
    try:
        mod = importlib.import_module(f"gout.{module}")
    except ModuleNotFoundError:
        mod = gout
    return getattr(mod, name)


class GoutError(AssertionError):
    pass


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, encoding="utf-8", check=True)
    return float(out.stdout.strip())


def stream_info(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,channels,sample_rate:format_tags", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(out.stdout)
    info = dict(data["streams"][0])
    info["tags"] = {k.lower(): v for k, v in (data.get("format", {}).get("tags") or {}).items()}
    return info


def samples(path: Path, channels: int = 1, rate: int = RATE) -> list[array.array]:
    """Decoded float samples, one array per channel."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "f32le",
                          "-ac", str(channels), "-ar", str(rate), "-"], capture_output=True, check=True).stdout
    data = array.array("f")
    data.frombytes(out)
    return [data[c::channels] for c in range(channels)]


def peak_time(path: Path, rate: int = RATE) -> float:
    (mono,) = samples(path, 1, rate)
    i = max(range(len(mono)), key=lambda k: abs(mono[k]))
    return i / rate


def hits(path: Path, threshold: float = 0.05, rate: int = RATE) -> list[tuple[float, float]]:
    """(time, level relative to the loudest) of separated impulses."""
    (mono,) = samples(path, 1, rate)
    top = max(abs(v) for v in mono) or 1.0
    found, k = [], 0
    while k < len(mono):
        if abs(mono[k]) > top * threshold:
            seg = mono[k:k + 400]
            j = max(range(len(seg)), key=lambda i: abs(seg[i]))
            found.append(((k + j) / rate, abs(seg[j]) / top))
            k += rate // 20
        else:
            k += 1
    return found


def peak_db(values) -> float:
    top = max((abs(v) for v in values), default=0.0)
    return 20 * math.log10(top) if top > 0 else float("-inf")


def loudness(path: Path) -> float:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                          "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8")
    text = out.stderr[out.stderr.rindex("{"):]
    return float(json.loads(text[:text.index("}") + 1])["input_i"])


def difference_peak_db(a: Path, b: Path) -> float:
    la, ra = samples(a, 2)
    lb, rb = samples(b, 2)
    n = max(len(la), len(lb))
    diff = []
    for x, y in ((la, lb), (ra, rb)):
        for i in range(n):
            diff.append((x[i] if i < len(x) else 0.0) - (y[i] if i < len(y) else 0.0))
    return peak_db(diff)


def stereo_correlation(path: Path, skip: float = 1.0) -> float:
    """How alike left and right are after `skip` seconds: 1 is mono, 0 is unrelated."""
    left, right = samples(path, 2)
    start = int(skip * RATE)
    l, r = left[start:], right[start:]
    den = math.sqrt(sum(a * a for a in l) * sum(b * b for b in r)) or 1.0
    return sum(a * b for a, b in zip(l, r)) / den


def thd_db(path: Path, f0: float, skip: float = 1.0, seconds: float = 2.0) -> float:
    """Everything that is not the f0 sine, relative to it, in dB (harmonics and noise)."""
    (mono,) = samples(path, 1)
    x = mono[int(skip * RATE):int((skip + seconds) * RATE)]
    w = 2 * math.pi * f0 / RATE
    s = sum(v * math.sin(w * i) for i, v in enumerate(x)) * 2 / len(x)
    c = sum(v * math.cos(w * i) for i, v in enumerate(x)) * 2 / len(x)
    residual = sum((v - s * math.sin(w * i) - c * math.cos(w * i)) ** 2 for i, v in enumerate(x)) / len(x)
    return 10 * math.log10(max(residual, 1e-30) / ((s * s + c * c) / 2))


def harmonic_db(path: Path, f0: float, n: int, skip: float = 1.0, seconds: float = 2.0) -> float:
    """Level of the n-th harmonic of f0 relative to the fundamental, in dB."""
    (mono,) = samples(path, 1)
    x = mono[int(skip * RATE):int((skip + seconds) * RATE)]

    def amplitude(f):
        w = 2 * math.pi * f / RATE
        s = sum(v * math.sin(w * i) for i, v in enumerate(x))
        c = sum(v * math.cos(w * i) for i, v in enumerate(x))
        return math.hypot(s, c)

    return 20 * math.log10(max(amplitude(n * f0), 1e-12) / amplitude(f0))


def dc_offset(path: Path, skip: float = 1.0) -> float:
    (mono,) = samples(path, 1)
    x = mono[int(skip * RATE):]
    return sum(x) / max(1, len(x))


def write_click(path: Path, seconds: float = 4.0, at: float = 2.0, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray(b"\x00\x00" * int(seconds * rate))
        i = int(at * rate)
        frames[2 * i:2 * i + 2] = struct.pack("<h", 30000)
        w.writeframes(bytes(frames))


def wait_for_exit(pid: int, fd: int, output: bytearray, seconds: float = 20.0) -> int | None:
    """Wait for a terminal test's child to leave, reading what it prints while it goes: a terminal
    nobody reads fills up (after about a kilobyte on macOS) and the child blocks half-way through a
    draw, for ever. Its exit code, or None when it outstayed its welcome and was killed."""
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                output.extend(os.read(fd, 65536))
            except OSError:  # the terminal went with the child
                pass
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return None


class Fixtures:
    """Audio made once per test run."""
    _dir: Path | None = None

    @classmethod
    def dir(cls) -> Path:
        if cls._dir is None:
            d = Path(tempfile.mkdtemp(prefix="gout-fixtures-"))
            write_click(d / "click.wav")
            ffmpeg("-i", str(d / "click.wav"), "-c:a", "libmp3lame", "-b:a", "128k", str(d / "click.mp3"))
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-c:a", "pcm_s16le", str(d / "tone.wav"))
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-c:a", "libmp3lame", "-b:a", "128k",
                   str(d / "tone.mp3"))
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=110:duration=4", "-ac", "2", "-ar", "44100",
                   "-c:a", "pcm_s16le", str(d / "bass.wav"))
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=3", "-c:a", "flac", str(d / "vox.flac"))
            ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.3:seed=1", "-ac", "2", "-ar", "48000",  # the same noise every run
                   "-c:a", "pcm_s16le", str(d / "noise.wav"))
            cls._dir = d
        return cls._dir


class GoutTest(unittest.TestCase):
    """A temporary working directory and a gout runner bound to it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gout-test-"))
        self.saved_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")  # never the user's own color.json
        self.saved_env = {name: os.environ.get(name) for name in ("GOUT_RECORDER", "GOUT_MONITOR")}
        os.environ["GOUT_RECORDER"] = "null"  # in-process code (the ui tests) never records a real input
        os.environ["GOUT_MONITOR"] = "none"   # nor plays one back
        for name in ("GNOME_TERMINAL_SERVICE", "GNOME_TERMINAL_SCREEN"):  # never the user's own window
            os.environ.pop(name, None)
        self.addons = self.tmp / "addons"
        self.addons.mkdir()
        self.fx = Fixtures.dir()
        self.cwd = self.tmp
        self.recorder = "null"
        self.more_env: dict[str, str] = {}

    def tearDown(self) -> None:
        if self.saved_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.saved_xdg
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if os.environ.get("GOUT_KEEP_TEST_DIRS"):
            print(f"\nkept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self) -> dict:
        env = dict(os.environ)
        env["GOUT_ADDONS"] = str(self.addons)  # never the user's own addons
        env["GOUT_PLAYER"] = "null"  # play in real time, in silence
        env["GOUT_RECORDER"] = self.recorder  # never the microphone: silence, or a file played as the input
        env.update(self.more_env)
        env["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        env.pop("COLUMNS", None)
        env.pop("GNOME_TERMINAL_SERVICE", None)  # a test must not make the user's terminal fullscreen
        env.pop("GNOME_TERMINAL_SCREEN", None)
        return env

    def gout(self, *args: str, ok: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run([*gout_cmd(), *args], cwd=cwd or self.cwd, env=self.env(),
                                capture_output=True, text=True, encoding="utf-8")
        if ok and result.returncode != 0:
            raise GoutError(f"gout {' '.join(args)} failed ({result.returncode}):\n{result.stdout}{result.stderr}")
        if not ok and result.returncode == 0:
            raise GoutError(f"gout {' '.join(args)} should have failed:\n{result.stdout}")
        return result

    def project(self, name: str = "song", *files: str, rate: int = RATE) -> Path:
        """A new project with the named fixtures added, autorender off, cwd moved into it."""
        self.gout("new", name, "--rate", str(rate))
        root = self.tmp / name
        self.cwd = root
        self.gout("set", "autorender", "off")
        for f in files:
            self.gout("add", str(self.fx / f))
        return root

    def dump(self) -> dict:
        return json.loads(self.gout("dump").stdout)
