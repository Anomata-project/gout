"""Recording from an input: a microphone, an audio interface, or on Linux what an output plays.

A capture program writes raw 32-bit float samples at the project rate into a pipe: pw-record
(PipeWire), parecord (PulseAudio) or arecord (ALSA) on Linux, ffmpeg with avfoundation on macOS and
dshow on Windows. A thread in gout reads the pipe as fast as it fills (a pipe nobody reads stalls
the capture after 64 KB), keeps the channels asked for, and appends them to a 32-bit float wav whose
header is brought up to date twice a second: a take cut short by a crash is still a playable file,
and gout scan registers it. The thread keeps the peak level for a meter as it goes.

The capture programs are asked for every channel the input has, in its own channel order, and gout
picks channels itself: asked for fewer, PipeWire and PulseAudio mix the channels down instead.

GOUT_RECORDER picks a capture program by name. GOUT_RECORDER=null records silence in real time;
GOUT_RECORDER=file:/some/path.wav plays that file in real time as if it were the input, which is
what the tests use, and the take ends where the file does. GOUT_RECORDER=loopback:SAMPLES[:BLOCKS]
is for recording along with the project (engine.py): the input hears the output SAMPLES later, and
the output starts BLOCKS blocks late, as real devices sometimes do.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from dataclasses import dataclass, replace
from pathlib import Path

from .core import config_home, detached, die, stop_process
from .media import FLOAT_WAV_HEADER, float_wav_header, probe

LINUX_BACKENDS = ("pw-record", "parecord", "arecord")
FFMPEG_BACKENDS = ("avfoundation", "dshow")
CLIP = 0.999  # a sample this close to full scale (-0.01 dBFS) counts as clipped
HEADER_EVERY = 0.5  # seconds between header updates while recording
SETTINGS_FILE = "recording.json"
MONITOR = ".monitor"  # the PulseAudio name of what a sink plays; gout uses it for pw-record too


@dataclass
class Input:
    name: str  # what the capture program is given
    label: str  # what people read
    channels: int = 0  # 0: not known before recording
    positions: str = ""  # the input's channel order in the capture program's words (FL,FR)
    monitor: bool = False  # what an output plays rather than a microphone
    default: bool = False  # the system's default input


# ---- which program, which inputs

def backends() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("avfoundation",)
    if os.name == "nt":
        return ("dshow",)
    return LINUX_BACKENDS


def installed(backend: str) -> bool:
    return shutil.which("ffmpeg" if backend in FFMPEG_BACKENDS else backend) is not None


def is_fake(backend: str) -> bool:
    """A test input that needs no sound hardware (null, file:, loopback:)."""
    return backend == "null" or backend.startswith(("file:", "loopback:"))


def choose_backend() -> str:
    wanted = os.environ.get("GOUT_RECORDER", "").strip()
    if wanted:
        if is_fake(wanted) or (wanted in backends() and installed(wanted)):
            return wanted
        die(f"GOUT_RECORDER={wanted}: use one of {', '.join(backends())}, null, file:PATH or loopback:SAMPLES,"
            " and it must be installed")
    for name in backends():
        if installed(name):
            return name
    die("nothing to record with: install pipewire (pw-record), pulseaudio-utils (parecord) or alsa-utils (arecord)")


def output_of(cmd: list[str], stderr: bool = False) -> str:
    env = dict(os.environ, LC_ALL="C")  # pactl translates its field names otherwise
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stderr if stderr else result.stdout


def list_inputs(backend: str) -> list[Input]:
    if backend == "null":
        return [Input("null", "silence (GOUT_RECORDER=null)", 2, default=True)]
    if backend.startswith("loopback:"):
        return [Input("loopback", "the output, heard back (GOUT_RECORDER=loopback)", 2, default=True)]
    if backend.startswith("file:"):
        path = Path(backend[5:])
        if not path.is_file():
            die(f"GOUT_RECORDER={backend}: no such file")
        return [Input(str(path), f"{path.name} (GOUT_RECORDER)", probe(path)["channels"], default=True)]
    if backend == "pw-record":
        for _ in range(2):
            found = pipewire_inputs(output_of(["pw-dump"]))
            if found:
                return found
        if shutil.which("pactl"):  # the same names; PulseAudio's channel words are not pw-record's
            return [replace(i, positions="") for i in pulse_inputs(output_of(["pactl", "list", "sources"]),
                                                                   output_of(["pactl", "info"]))]
        return []
    if backend == "parecord":
        return pulse_inputs(output_of(["pactl", "list", "sources"]), output_of(["pactl", "info"]))
    if backend == "arecord":
        return alsa_inputs(output_of(["arecord", "-L"]))
    if backend == "avfoundation":
        return avfoundation_inputs(output_of(["ffmpeg", "-hide_banner", "-f", "avfoundation",
                                              "-list_devices", "true", "-i", ""], stderr=True))
    if backend == "dshow":
        return dshow_inputs(output_of(["ffmpeg", "-hide_banner", "-list_devices", "true",
                                       "-f", "dshow", "-i", "dummy"], stderr=True))
    die(f"unknown capture program {backend}")


def pipewire_inputs(dump: str) -> list[Input]:
    """Sources, then what each sink plays, from pw-dump's JSON."""
    try:
        objects = json.loads(dump or "[]")
    except json.JSONDecodeError:
        return []
    default = ""
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        for entry in obj.get("metadata") or []:
            if entry.get("key") == "default.audio.source":
                value = entry.get("value")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = {}
                default = (value or {}).get("name", "") if isinstance(value, dict) else ""
    sources, monitors = [], []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name, kind = props.get("node.name"), props.get("media.class", "")
        if not name:
            continue
        channels = int(props.get("audio.channels") or 0)
        positions = ",".join(re.findall(r"[A-Za-z0-9_]+", str(props.get("audio.position") or "")))
        label = props.get("node.description") or props.get("node.nick") or name
        if kind in ("Audio/Source", "Audio/Source/Virtual", "Audio/Duplex"):
            sources.append(Input(name, label, channels, positions, default=name == default))
        if kind in ("Audio/Sink", "Audio/Duplex"):
            monitors.append(Input(name + MONITOR, f"what {label} plays", channels, positions, monitor=True))
    return sources + monitors


def pulse_inputs(sources: str, info: str) -> list[Input]:
    """From pactl list sources and pactl info (in the C locale)."""
    found = re.search(r"^Default Source:\s*(\S+)", info, re.M)
    default = found.group(1) if found else ""
    out: list[Input] = []
    for block in re.split(r"^Source #\d+\s*$", sources, flags=re.M)[1:]:
        field = {m.group(1): m.group(2).strip() for m in re.finditer(r"^\s*([A-Za-z ]+):\s*(.*)$", block, re.M)}
        name = field.get("Name")
        if not name:
            continue
        spec = re.search(r"(\d+)ch", field.get("Sample Specification", ""))
        monitor = field.get("Monitor of Sink", "n/a") != "n/a"
        label = field.get("Description") or name
        if monitor and label.startswith("Monitor of "):
            label = f"what {label[len('Monitor of '):]} plays"
        out.append(Input(name, label, int(spec.group(1)) if spec else 0, field.get("Channel Map", ""),
                         monitor=monitor, default=name == default))
    return sorted(out, key=lambda i: i.monitor)


def alsa_inputs(listing: str) -> list[Input]:
    """From arecord -L: a name at the start of a line, its description indented below. Only sound
    servers and cards: the list also has resamplers and up/downmix plugins."""
    out: list[Input] = []
    for block in re.split(r"\n(?=\S)", listing.strip()):
        lines = block.splitlines()
        name = lines[0].strip() if lines else ""
        if not re.fullmatch(r"default|pipewire|pulse|jack|(sysdefault|hw|plughw|dsnoop):\S+", name):
            continue
        label = " ".join(line.strip() for line in lines[1:]) or name
        out.append(Input(name, f"{label} ({name})" if label != name else name, default=name == "default"))
    return out


def avfoundation_inputs(listing: str) -> list[Input]:
    """From ffmpeg -f avfoundation -list_devices true -i "": the audio half of the list."""
    audio = listing.split("AVFoundation audio devices:", 1)
    out = [Input("default", "the default input (System Settings, Sound)", default=True)]
    if len(audio) == 2:
        out += [Input(m.group(1).strip(), m.group(1).strip()) for m in re.finditer(r"\]\s*\[\d+\]\s*(.+)$", audio[1], re.M)]
    return out


def dshow_inputs(listing: str) -> list[Input]:
    """From ffmpeg -list_devices true -f dshow -i dummy: "Name" (audio) lines (ffmpeg 5 and later),
    or the names under "DirectShow audio devices" (older)."""
    names = re.findall(r'"([^"]+)"\s+\(audio\)', listing)
    if not names and "DirectShow audio devices" in listing:
        section = listing.split("DirectShow audio devices", 1)[1]
        names = [n for n in re.findall(r'\]\s+"([^"]+)"', section) if not n.startswith("@device")]
    return [Input(name, name, default=i == 0) for i, name in enumerate(names)]


# ---- the input this computer records from

def settings_path() -> Path:
    return config_home() / SETTINGS_FILE


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(**changes) -> None:
    data = load_settings()
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def find_input(inputs: list[Input], spec: str) -> Input:
    """An input by its number in the list, its name, or a piece of its label that only it has."""
    if spec.isdigit() and 1 <= int(spec) <= len(inputs):
        return inputs[int(spec) - 1]
    for field in ("name", "label"):
        exact = [i for i in inputs if getattr(i, field).lower() == spec.lower()]
        if len(exact) == 1:
            return exact[0]
    matches = [i for i in inputs if spec.lower() in i.label.lower() or spec.lower() in i.name.lower()]
    if len(matches) == 1:
        return matches[0]
    listing = "; ".join(f"{k} {i.label}" for k, i in enumerate(inputs, 1))
    die(f"{'more than one input matches' if matches else 'no input matches'} {spec!r}: {listing or 'there are none'}")


def current_input(inputs: list[Input], spec: str | None = None) -> tuple[Input, str]:
    """The input to record from: spec when given, else the one picked for this computer, else the
    system default. The second value explains a picked input that is not there any more."""
    if not inputs:
        die("no inputs found to record from (gout inputs lists them)")
    if spec:
        return find_input(inputs, spec), ""
    fallback = next((i for i in inputs if i.default), inputs[0])
    saved = load_settings().get("input")
    if not saved:
        return fallback, ""
    for item in inputs:
        if item.name == saved:
            return item, ""
    return fallback, f"the input picked for this computer is not here ({saved}); using {fallback.label}"


# ---- one take

def default_output(backend: str) -> str:
    """The name of the output the system plays to, which calibrations are kept for on Linux."""
    if backend == "pw-record":
        try:
            objects = json.loads(output_of(["pw-dump"]) or "[]")
        except json.JSONDecodeError:
            objects = []
        for obj in objects:
            for entry in (obj.get("metadata") or []) if obj.get("type") == "PipeWire:Interface:Metadata" else []:
                value = entry.get("value")
                if entry.get("key") == "default.audio.sink" and isinstance(value, dict) and value.get("name"):
                    return value["name"]
    if backend in ("pw-record", "parecord") and shutil.which("pactl"):
        found = re.search(r"^Default Sink:\s*(\S+)", output_of(["pactl", "info"]), re.M)
        if found:
            return found.group(1)
    return "default"


# ---- hearing the input while recording

MONITOR_LATENCY_MS = 10  # asked of PipeWire; measured on an ALSA laptop jack: 256-sample cycles, no dropouts


def pactl_block(text: str, kind: str, name: str) -> str:
    """The part of `pactl list sinks` or `pactl list sources` about the one called name."""
    for chunk in re.split(rf"^{kind} #", text, flags=re.M):
        if re.search(rf"^\s*Name:\s*{re.escape(name)}\s*$", chunk, re.M):
            return chunk
    return ""


def speaker_output(text: str, sink: str) -> bool:
    """Whether a sink's active port is a loudspeaker, from `pactl list sinks`."""
    block = pactl_block(text, "Sink", sink)
    active = re.search(r"^\s*Active Port:\s*(\S+)", block, re.M)
    if not active:
        return False
    port = re.search(rf"^\s*{re.escape(active.group(1))}:.*\(type: (\w+)", block, re.M)
    return (port.group(1) if port else active.group(1)).lower().startswith("speaker")


def muted_input(text: str, source: str) -> bool:
    """Whether the system has muted a source, from `pactl list sources`."""
    found = re.search(r"^\s*Mute:\s*(\w+)", pactl_block(text, "Source", source), re.M)
    return bool(found) and found.group(1).lower() == "yes"


def input_is_muted(backend: str, device: Input) -> bool:
    if backend not in ("pw-record", "parecord") or not shutil.which("pactl"):
        return False
    return muted_input(output_of(["pactl", "list", "sources"]), device.name)


def monitor_plan(backend: str, device: Input, output: str) -> tuple[list[str] | None, str]:
    """How the input gets to the headphones while recording: PipeWire's pw-loopback from the input
    to the output gout plays to. (None, why) when it does not: not on the speakers, where a
    microphone would howl, and not for what an output plays, which would echo round and round.
    GOUT_MONITOR=none switches it off, GOUT_MONITOR=cmd:PROGRAM runs PROGRAM instead (tests)."""
    wanted = os.environ.get("GOUT_MONITOR", "").strip()
    if wanted == "none":
        return None, ""
    if device.monitor:
        return None, "you do not hear this input while recording: it is what an output plays, it would echo"
    if wanted.startswith("cmd:"):
        program = wanted[4:]
    elif is_fake(backend):
        return None, ""
    elif backend != "pw-record":
        return None, "you do not hear the input while recording: that needs PipeWire (pw-loopback) for now"
    else:
        program = shutil.which("pw-loopback") or ""
        if not program:
            return None, "you do not hear the input while recording: pw-loopback is not installed (pipewire-bin)"
        if shutil.which("pactl") and speaker_output(output_of(["pactl", "list", "sinks"]), output):
            return None, ("you do not hear the input while recording: the sound goes to the speakers and a "
                          "microphone would howl; with headphones in you do")
    return ([program, "-n", "gout-monitor", "-C", device.name, "-P", output, "-l", str(MONITOR_LATENCY_MS)],
            f"you hear the input in your headphones too, about {MONITOR_LATENCY_MS} ms late (-M: not)")


class Monitor:
    """The input played to the output while a take runs. Stops with the take, with ctrl-c (same
    process group) and, on Linux, when gout itself dies."""

    def __init__(self, cmd: list[str] | None):
        self.cmd, self.proc = cmd, None

    def start(self) -> "Monitor":
        if self.cmd:
            try:
                self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, preexec_fn=die_with_parent if sys.platform.startswith("linux") else None)
            except OSError:
                self.proc = None
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def die_with_parent() -> None:
    """In the child before it runs: ask Linux to end it when gout ends, however gout ends."""
    try:
        import ctypes
        import signal
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def check_channels(device: Input, first: int, count: int) -> list[int]:
    """The channels to keep, 0-based, after checking the input has them."""
    last = first + count - 1
    if first < 1:
        die("channels count from 1")
    if device.channels and last > device.channels:
        die(f"{device.label} has {device.channels} channel{'' if device.channels == 1 else 's'}:"
            f" there is no channel {last}")
    return list(range(first - 1, last))


def capture_plan(backend: str, device: Input, rate: int, first: int, count: int) -> tuple[list[str], int, list[int]]:
    """The capture command, how many channels it delivers, and which of them (0-based) to keep."""
    wanted = check_channels(device, first, count)
    last = first + count - 1
    if backend in ("pw-record", "parecord", "arecord"):
        stream = device.channels or max(2, last)
        mapped = bool(device.positions) and device.channels == stream
        if backend == "pw-record":
            target = device.name[:-len(MONITOR)] if device.monitor and device.name.endswith(MONITOR) else device.name
            cmd = ["pw-record", "--target", target, *(["-P", "{ stream.capture.sink=true }"] if device.monitor else []),
                   "--rate", str(rate), "--channels", str(stream), *(["--channel-map", device.positions] if mapped else []),
                   "--format", "f32", "-"]
        elif backend == "parecord":
            cmd = ["parecord", "--raw", f"--device={device.name}", "--format=float32le", f"--rate={rate}",
                   f"--channels={stream}", *([f"--channel-map={device.positions}"] if mapped else []),
                   "--latency-msec=20"]  # its default buffer loses the last second when stopped
        else:
            cmd = ["arecord", "-q", "-t", "raw", "-f", "FLOAT_LE", "-r", str(rate), "-c", str(stream), "-D", device.name]
        return cmd, stream, wanted
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    layout = "mono" if count == 1 else "stereo"
    pick = f"pan={layout}|" + "|".join(f"c{k}=c{c}" for k, c in enumerate(wanted))
    out = ["-af", pick, "-ar", str(rate), "-f", "f32le", "-"]
    if backend == "null" or backend.startswith("loopback:"):  # nothing plays, so a loopback hears nothing
        return [*ffmpeg, "-re", "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}", "-f", "f32le", "-"], count, list(range(count))
    if backend.startswith("file:"):
        return [*ffmpeg, "-re", "-i", device.name, *out], count, list(range(count))
    if backend == "avfoundation":
        return [*ffmpeg, "-f", "avfoundation", "-i", f":{device.name}", *out], count, list(range(count))
    if backend == "dshow":
        return [*ffmpeg, "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={device.name}", *out], count, list(range(count))
    die(f"unknown capture program {backend}")


def level_db(peak: float) -> float:
    return 20 * math.log10(peak) if peak > 0 else float("-inf")


def meter(peak: float, width: int = 20, floor_db: float = -60.0) -> str:
    """A level bar from floor_db to 0 dBFS with the level in figures after it."""
    db = level_db(peak)
    cells = 0 if db == float("-inf") else max(0, min(width, round(width * (db - floor_db) / -floor_db)))
    figure = "  -inf" if db == float("-inf") else f"{db:6.1f}"
    return "█" * cells + "·" * (width - cells) + f" {figure} dB" + ("  CLIP" if peak >= CLIP else "")


class TakeWriter:
    """Appends frames to a 32-bit float wav and keeps its header up to date as it goes, so the file
    plays whatever happens to gout. Picks channels out of interleaved raw frames and keeps levels."""

    def __init__(self, path: Path, rate: int, stream_channels: int, picks: list[int], max_frames: int | None = None):
        self.path, self.rate, self.stream, self.picks = path, rate, stream_channels, picks
        self.count, self.max_frames = len(picks), max_frames
        self.frames = 0
        self.peak = 0.0  # since take_peak() last asked
        self.top = 0.0  # the whole take
        self.clipped = False
        self.full = False  # max_frames reached
        self.rest = b""
        self.file = open(path, "wb")
        self.file.write(float_wav_header(self.count, rate, 0))
        self.header_at = time.monotonic()

    def write(self, data: bytes) -> bool:
        """Take raw little-endian float frames (any amount); False once max_frames is reached."""
        if self.full:
            return False
        width = 4 * self.stream
        data = self.rest + bytes(data)
        usable = len(data) // width * width
        self.rest = data[usable:]
        block = array("f")
        block.frombytes(data[:usable])
        if sys.byteorder == "big":
            block.byteswap()
        frames = usable // width
        if self.max_frames is not None and self.frames + frames >= self.max_frames:
            frames = self.max_frames - self.frames
            del block[frames * self.stream:]
            self.full = True
        out = pick_channels(block, self.stream, self.picks)
        if out:
            level = max(max(out), -min(out))
            self.peak = max(self.peak, level)
            self.top = max(self.top, level)
            self.clipped = self.clipped or level >= CLIP
        if sys.byteorder == "big":
            out.byteswap()
        self.file.write(out.tobytes())
        self.frames += frames
        if time.monotonic() - self.header_at >= HEADER_EVERY:
            self.write_header()
        return not self.full

    def write_header(self) -> None:
        self.header_at = time.monotonic()
        data = self.frames * self.count * 4
        try:
            self.file.flush()
            self.file.seek(4)
            self.file.write((36 + data).to_bytes(4, "little"))
            self.file.seek(FLOAT_WAV_HEADER - 4)
            self.file.write(data.to_bytes(4, "little"))
            self.file.seek(0, os.SEEK_END)
            self.file.flush()
        except (OSError, ValueError):
            pass

    def take_peak(self) -> float:
        peak, self.peak = self.peak, 0.0
        return peak

    def close(self) -> None:
        if not self.file.closed:
            self.write_header()
            self.file.close()


class Recorder:
    """Records one take into path through a capture program, without playing anything, until
    stop(), max_frames, or the input ends."""

    def __init__(self, path: Path, rate: int, device: Input, backend: str, first: int = 1, count: int = 1,
                 max_frames: int | None = None):
        self.path, self.rate, self.device, self.backend = path, rate, device, backend
        self.max_frames = max_frames
        self.args, self.stream, self.picks = capture_plan(backend, device, rate, first, count)
        self.writer: TakeWriter | None = None
        self.error = ""
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.errlog = None
        self.stopping = False

    def start(self) -> "Recorder":
        self.writer = TakeWriter(self.path, self.rate, self.stream, self.picks, self.max_frames)
        self.errlog = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=self.errlog, **detached())
        except OSError as exc:
            self.writer.close()
            self.path.unlink(missing_ok=True)
            die(f"{self.args[0]} did not start: {exc}")
        self.thread = threading.Thread(target=self.pump, name="gout-record", daemon=True)
        self.thread.start()
        return self

    def pump(self) -> None:
        try:
            while True:
                data = self.proc.stdout.read1(1 << 16)
                if not data or not self.writer.write(data):
                    break
        except (OSError, ValueError) as exc:  # a full disk, or the file closed under it
            self.error = str(exc)
        finally:
            self.writer.write_header()
            stop_process(self.proc)

    # what the command and the ui read while recording and after
    frames = property(lambda self: self.writer.frames if self.writer else 0)
    top = property(lambda self: self.writer.top if self.writer else 0.0)
    clipped = property(lambda self: bool(self.writer and self.writer.clipped))
    full = property(lambda self: bool(self.writer and self.writer.full))
    correction_ms = 0
    note = ""

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def seconds(self) -> float:
        return self.frames / self.rate

    def take_peak(self) -> float:
        return self.writer.take_peak() if self.writer else 0.0

    def stop(self) -> int:
        """Stop the capture, finish the file, and return the frames recorded."""
        self.stopping = True
        if self.proc is not None:
            stop_process(self.proc)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.writer is not None:
            self.writer.close()
        return self.frames

    def ended_by_itself(self) -> bool:
        """The input stopped on its own: not stop(), not max_frames."""
        return not self.stopping and not self.full

    def complaint(self) -> str:
        """What the capture program said, or the error that stopped the take."""
        if self.error:
            return self.error
        if self.errlog is None:
            return ""
        self.errlog.seek(0)
        lines = [line.strip() for line in self.errlog.read().decode(errors="replace").splitlines() if line.strip()]
        return " / ".join(lines[-3:])


def pick_channels(block: array, stream: int, picks: list[int]) -> array:
    if picks == list(range(stream)):
        return block
    if len(picks) == 1:
        return block[picks[0]::stream]
    out = array("f", bytes(4 * (len(block) // stream) * len(picks)))
    for k, channel in enumerate(picks):
        out[k::len(picks)] = block[channel::stream]
    return out
