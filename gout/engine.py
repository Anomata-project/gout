"""Recording while the project plays: one PortAudio duplex stream, in a process of its own.

gout record starts `gout _engine`, writes the job to its stdin as one JSON line, and reads JSON
lines back: started, a level ten times a second, done. A line "stop" on stdin, or stdin closing
because gout went away, ends the take; the file is a playable wav at every moment either way.

The child decodes what to play with ffmpeg into a buffer ahead of the stream, then loops: write a
block, count frames the device has captured minus frames it has played, read a block, append it to
the take. Played frames are what was written minus what still waits in the output buffer, and the
buffer's size is its free space right after the stream starts. The output can start a block or two
later than the input from one take to the next; that count moves with it. The take lags the project
by the count (its most common value over the take) plus a constant for the input and output: the
latency outside the buffers, which gout record calibrate measures. Measured with a loopback that
held to the sample in 45 takes out of 45, all cores busy (see notes).

A test input stands in for PortAudio: GOUT_RECORDER=null (silence), file:PATH (the file as the
input) or loopback:SAMPLES[:BLOCKS] (the input hears the output SAMPLES later, the output starts
BLOCKS blocks late), all in real time.
"""
from __future__ import annotations

import collections
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from pathlib import Path

from . import portaudio
from .core import detached, die, GoutError, gout_command, stop_process
from .recorder import check_channels, default_output, Input, is_fake, load_settings, save_settings, TakeWriter

BLOCK = 2048  # frames per read and write; a calibration holds for one block size
AHEAD_SECONDS = 4.0  # decoded playback kept ready
LEVEL_EVERY = 0.1
CLICK_LENGTH = 48  # samples of each calibration click
CLICK_TIMES = [1.0 + 0.75 * k for k in range(10)]  # a second of quiet first, to hear the noise
CLICK_WINDOW = 0.65  # seconds after each click to look for it


def duplex_available(backend: str) -> bool:
    """Whether gout can play while it records: PortAudio is there, or a test input stands in."""
    if is_fake(backend):
        return os.environ.get("GOUT_PORTAUDIO", "").strip() != "none"
    return portaudio.available()


def install_hint() -> str:
    if sys.platform == "darwin":
        return "brew install portaudio"
    if os.name == "nt":
        return "the gout installer includes it"
    return "sudo apt install libportaudio2, or your distribution's portaudio package"


# ---- calibration: the latency outside the buffers, per input, output, rate and block size

def calibration_key(input_name: str, output_name: str, rate: int, block: int) -> str:
    return f"{input_name} -> {output_name} @ {rate} Hz / {block}"


def calibration(key: str) -> int | None:
    value = (load_settings().get("calibration") or {}).get(key)
    return int(value["samples"]) if isinstance(value, dict) and "samples" in value else None


def save_calibration(key: str, samples: int, rate: int, spread: int) -> None:
    table = dict(load_settings().get("calibration") or {})
    table[key] = {"samples": samples, "ms": round(samples * 1000 / rate, 2), "spread_samples": spread,
                  "date": time.strftime("%Y-%m-%d")}
    save_settings(calibration=table)


def default_constant(route: str, block: int) -> int:
    """Before calibrating: through PipeWire's ALSA plugin one block sits outside the counts
    (measured with a loopback); elsewhere nothing is known."""
    return block if route == "pulse" else 0


# ---- the parent side

class Engine:
    """Records a take while the project plays, from the parent's side. Offers what recorder.Recorder
    offers, so gout record and the ui treat both alike, plus correction_ms and notes."""

    def __init__(self, path: Path, rate: int, device: Input, backend: str, first: int = 1, count: int = 1,
                 max_frames: int | None = None, play: list[str] | None = None, output_name: str = "default",
                 clicks: list[float] | None = None):
        self.path, self.rate, self.device, self.backend = path, rate, device, backend
        self.picks = check_channels(device, first, count)
        self.job = {
            "path": str(path), "rate": rate, "block": BLOCK, "backend": backend,
            "input": {"name": device.name, "label": device.label, "channels": device.channels,
                      "monitor": device.monitor, "picks": self.picks},
            "output": output_name, "play": play, "clicks": clicks,
            "max_seconds": None if max_frames is None else max_frames / rate,
        }
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.errlog = None
        self.started: dict = {}
        self.result: dict = {}
        self.error = ""
        self.frames = 0
        self.peak = 0.0
        self.stopping = False
        self.ready = threading.Event()

    def start(self) -> "Engine":
        self.errlog = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([*gout_command(), "_engine"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, text=True, encoding="utf-8", **detached())
        self.proc.stdin.write(json.dumps(self.job) + "\n")
        self.proc.stdin.flush()
        self.thread = threading.Thread(target=self.listen, name="gout-engine", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=30)
        if not self.started:
            self.stop()
            self.path.unlink(missing_ok=True)
            die(f"recording along did not start: {self.complaint() or 'no answer from the engine'}")
        return self

    def listen(self) -> None:
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("event")
            if kind == "started":
                self.started = event
                self.rate = event["rate"]
                self.ready.set()
            elif kind == "level":
                self.frames = event["frames"]
                self.peak = max(self.peak, event["peak"])
            elif kind == "done":
                self.result = event
                self.frames = event["frames"]
            elif kind == "error":
                self.error = event["message"]
        self.ready.set()

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def seconds(self) -> float:
        return self.frames / self.rate

    def take_peak(self) -> float:
        peak, self.peak = self.peak, 0.0
        return peak

    top = property(lambda self: self.result.get("top", 0.0))
    clipped = property(lambda self: bool(self.result.get("clipped")))
    full = property(lambda self: bool(self.result.get("full")))

    def stop(self) -> int:
        self.stopping = True
        if self.proc is not None:
            try:
                self.proc.stdin.write("stop\n")
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                stop_process(self.proc)
        if self.thread is not None:
            self.thread.join(timeout=5)
        return self.frames

    def ended_by_itself(self) -> bool:
        """As Recorder's: the engine had ended before stop() was asked for, and not at max_frames."""
        return not self.running() and not self.stopping and not self.full

    def complaint(self) -> str:
        if self.error:
            return self.error
        if self.errlog is None:
            return ""
        self.errlog.seek(0)
        lines = [line.strip() for line in self.errlog.read().decode(errors="replace").splitlines() if line.strip()]
        return " / ".join(lines[-3:])

    # ---- what the take needs to line up

    def key(self) -> str:
        return calibration_key(self.started.get("input", self.device.name), self.started.get("output", "default"),
                               self.rate, BLOCK)

    def lag_samples(self) -> tuple[int, bool]:
        """How far the take lags what played, and whether a calibration says so."""
        constant = calibration(self.key())
        measured = int(self.result.get("lag", 0))
        if constant is None:
            return default_constant(self.started.get("route", ""), BLOCK) + measured, False
        return constant + measured, True

    @property
    def correction_ms(self) -> int:
        return round(self.lag_samples()[0] * 1000 / self.rate) if self.result else 0

    @property
    def note(self) -> str:
        if not self.result:
            return ""
        samples, calibrated = self.lag_samples()
        how = "calibrated" if calibrated else "not calibrated for this input and output: gout record calibrate"
        lines = [f"in time with what played: {samples * 1000 / self.rate:.1f} ms of latency taken off ({how})"]
        if self.result.get("dropouts"):
            lines.append(f"{self.result['dropouts']} dropout{'s' if self.result['dropouts'] > 1 else ''} while"
                         " recording: after the first, the take can be out of time")
        if self.result.get("gaps"):
            lines.append("the live mix fell behind while playing, so the take can be out of time after that:"
                         " render first (gout mix) and record against master.wav")
        return "\n".join(lines)


# ---- the child

class PlaySource:
    """Stereo float frames to play: the project decoded by ffmpeg ahead of time, clicks for a
    calibration, or silence. fill() never waits: short of data it plays silence and counts a gap."""

    def __init__(self, job: dict, rate: int):
        self.rate = rate
        self.data = bytearray()
        self.lock = threading.Lock()
        self.ended = True
        self.gaps = 0
        self.proc = None
        if job.get("clicks"):
            self.data = bytearray(click_train(job["clicks"], rate).tobytes())
        elif job.get("play"):
            self.ended = False
            self.proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *job["play"],
                                          "-ac", "2", "-ar", str(rate), "-f", "f32le", "-"],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self.pump, daemon=True).start()

    def pump(self) -> None:
        limit = int(AHEAD_SECONDS * self.rate) * 8
        while True:
            with self.lock:
                full = len(self.data) >= limit
            if full:
                time.sleep(0.05)
                continue
            chunk = self.proc.stdout.read1(1 << 16)
            if not chunk:
                break
            with self.lock:
                self.data += chunk
        self.ended = True

    def prefill(self, seconds: float = 1.0, timeout: float = 20.0) -> None:
        """Wait for a second of decoded audio (a live mix with a reverb takes a moment to start)."""
        deadline = time.monotonic() + timeout
        while not self.ended and len(self.data) < seconds * self.rate * 8 and time.monotonic() < deadline:
            time.sleep(0.02)

    def fill(self, out, frames: int) -> None:
        want = frames * 8
        with self.lock:
            chunk = self.data[:want]
            del self.data[:want]
        if len(chunk) < want and not self.ended:
            self.gaps += 1
        ctypes.memmove(out, bytes(chunk) + bytes(want - len(chunk)), want)

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()


def click_shape() -> list[float]:
    """One calibration click: a cycle of a sine CLICK_LENGTH samples long at -6 dBFS."""
    import math
    return [0.5 * math.sin(2 * math.pi * k / CLICK_LENGTH) for k in range(CLICK_LENGTH)]


def click_train(times: list[float], rate: int) -> array:
    """Stereo clicks at the times, with half a second of silence after the last."""
    end = int((max(times) + 0.5) * rate)
    frames = array("f", bytes(8 * end))
    for t in times:
        at = int(round(t * rate))
        for k, value in enumerate(click_shape()):
            frames[2 * (at + k)] = frames[2 * (at + k) + 1] = value
    if sys.byteorder == "big":
        frames.byteswap()
    return frames


def onset(values, fraction: float = 0.5) -> int:
    """The first sample reaching fraction of the loudest one: where a click starts, whatever its level."""
    top = max(max(values), -min(values))
    return next(i for i, v in enumerate(values) if abs(v) >= fraction * top)


def click_offsets(take: array, rate: int, times: list[float]) -> tuple[list[int], float]:
    """How many samples after it was played each click came back in the take, for the clicks heard
    well above the noise before the first one; and how far above that noise the loudest click was."""
    quiet = take[int(0.1 * rate):int((times[0] - 0.1) * rate)]
    floor = max(max(quiet), -min(quiet)) if quiet else 0.0
    reference = onset(array("f", click_shape()))  # as float32, like the take: rounding moves the crossing
    offsets, loudest = [], 0.0
    for t in times:
        start = int(round(t * rate))
        window = take[start:start + int(CLICK_WINDOW * rate)]
        if not window:
            continue
        top = max(max(window), -min(window))
        loudest = max(loudest, top)
        if top >= max(8 * floor, 1e-4):
            offsets.append(onset(window) - reference)
    return offsets, (loudest / floor if floor else float("inf"))


def output_for(backend: str) -> str:
    """The output a take plays to, as calibrations name it."""
    if backend in ("pw-record", "parecord") and os.environ.get("PULSE_SINK"):
        return os.environ["PULSE_SINK"]
    return default_output(backend)


class FakeStream:
    """A duplex device in software, in real time, for the tests: see the module docstring."""

    def __init__(self, backend: str, channels: int, rate: int, block: int):
        self.rate, self.block, self.input_channels = rate, block, channels
        self.latency = 0
        self.late = block  # a device plays a block or so after it is written: its buffer
        self.mic: array | None = None
        if backend.startswith("loopback:"):
            parts = backend.split(":")[1:]
            self.latency = int(parts[0])
            self.late += int(parts[1]) * block if len(parts) > 1 else 0
        elif backend.startswith("file:"):
            raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", backend[5:], "-ac", str(channels),
                                  "-ar", str(rate), "-f", "f32le", "-"], capture_output=True, check=True).stdout
            self.mic = array("f")
            self.mic.frombytes(raw)
        self.size = 4 * block
        self.loop = backend.startswith("loopback:")
        self.out = array("f")
        self.written = self.read_frames = 0
        self.t0 = 0.0

    def now(self) -> int:
        return int((time.monotonic() - self.t0) * self.rate)

    def played(self) -> int:
        return max(0, min(self.written, self.now() - self.late))

    def start(self) -> None:
        self.t0 = time.monotonic()

    def write_available(self) -> int:
        return self.size - (self.written - self.played())

    def read_available(self) -> int:
        return max(0, self.now() - self.read_frames)

    def write(self, address: int) -> bool:
        while self.write_available() < self.block:
            time.sleep(0.002)
        underflow = self.now() - self.late > self.written
        chunk = array("f")
        chunk.frombytes(ctypes.string_at(address, 8 * self.block))
        self.out.extend(chunk)
        self.written += self.block
        return not underflow

    def read(self, address: int) -> bool:
        while self.read_available() < self.block:
            time.sleep(0.002)
        n, ch = self.block, self.input_channels
        block = array("f", bytes(4 * n * ch))
        if self.mic is not None:
            piece = self.mic[self.read_frames * ch:(self.read_frames + n) * ch]
            block[:len(piece)] = piece
        if self.loop:
            first = self.read_frames - self.latency - self.late  # the output frame the input hears now
            for i in range(n):
                j = first + i
                if 0 <= j < self.written:
                    for c in range(ch):
                        block[i * ch + c] += self.out[2 * j + min(c, 1)]
        ctypes.memmove(address, block.tobytes(), 4 * n * ch)
        self.read_frames += n
        return True

    @property
    def ended(self) -> bool:
        """A file as the input ends where the file does."""
        return self.mic is not None and self.read_frames * self.input_channels >= len(self.mic)

    def close(self) -> None:
        pass


def open_stream(job: dict):
    """The stream for the job and what it goes through: (stream, route, input name, output name)."""
    backend, device = job["backend"], job["input"]
    wanted = max(device["picks"]) + 1
    if is_fake(backend):
        channels = device["channels"] or max(2, wanted)
        return FakeStream(backend, channels, job["rate"], job["block"]), "fake", device["name"], job["output"]
    route = "pulse" if backend in ("pw-record", "parecord") else "portaudio"
    if route == "pulse":
        os.environ["PULSE_SOURCE"] = device["name"]  # the ALSA pulse device records from this source
    lib = portaudio.initialize()
    found = portaudio.devices(lib)
    by_index = {d.index: d for d in found}
    if route == "pulse":
        pulse = next((d for d in found if d.name == "pulse" and d.inputs and d.outputs), None)
        if pulse is None:
            route = "portaudio"
        else:
            return (portaudio.Stream(lib, pulse, device["channels"] or max(2, wanted), pulse, job["rate"], job["block"]),
                    route, device["name"], job["output"])
    input_dev = pick_input(lib, found, device["name"])
    output_dev = by_index.get(lib.Pa_GetHostApiInfo(lib.Pa_GetDeviceInfo(input_dev.index).contents.hostApi)
                              .contents.defaultOutputDevice) or by_index.get(lib.Pa_GetDefaultOutputDevice())
    if output_dev is None:
        die("PortAudio finds no output to play to")
    if wanted > input_dev.inputs:
        die(f"{input_dev.name} has {input_dev.inputs} channels: there is no channel {wanted}")
    channels = min(input_dev.inputs, max(wanted, 2))
    return (portaudio.Stream(lib, input_dev, channels, output_dev, job["rate"], job["block"]),
            route, input_dev.name, output_dev.name)


def pick_input(lib, found: list, name: str):
    """The PortAudio device for an input by the name gout inputs gave it: the same name (MME cuts
    names at 31 characters), preferring WASAPI on Windows; the default input for 'default'."""
    inputs = [d for d in found if d.inputs > 0]
    if name != "default":
        order = ["Windows WASAPI", "Windows DirectSound", "MME"]
        matches = [d for d in inputs if d.name == name or (len(d.name) >= 31 and name.startswith(d.name))]
        matches.sort(key=lambda d: order.index(d.host_api) if d.host_api in order else len(order))
        if matches:
            return matches[0]
    index = lib.Pa_GetDefaultInputDevice()
    for d in inputs:
        if d.index == index:
            return d
    if not inputs:
        die("PortAudio finds no input to record from")
    return inputs[0]


def child_main() -> int:
    """gout _engine: run one job from stdin; see the module docstring."""
    stop = threading.Event()

    def emit(**fields) -> None:
        try:
            print(json.dumps(fields), flush=True)
        except (OSError, ValueError):  # gout went away: finish the take
            stop.set()

    try:
        job = json.loads(sys.stdin.readline())
    except json.JSONDecodeError as exc:
        emit(event="error", message=f"bad job: {exc}")
        return 1

    def watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        stop.set()  # "stop", or gout went away

    threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        stream, route, input_name, output_name = open_stream(job)
    except GoutError as exc:
        emit(event="error", message=str(exc))
        return 1
    rate, block = stream.rate, job["block"]
    source = PlaySource(job, rate)
    source.prefill()
    picks = job["input"]["picks"]
    max_frames = None if job["max_seconds"] is None else round(job["max_seconds"] * rate)
    writer = TakeWriter(Path(job["path"]), rate, stream.input_channels, picks, max_frames)
    out_buf = (ctypes.c_float * (2 * block))()
    in_buf = (ctypes.c_float * (stream.input_channels * block))()
    lags: collections.Counter = collections.Counter()
    dropouts = written = read = 0
    clicks_end = len(click_train(job["clicks"], rate)) // 2 + rate // 2 if job.get("clicks") else None
    error = ""
    emit(event="started", rate=rate, route=route, input=input_name, output=output_name)
    try:
        stream.start()
        size = stream.write_available()  # nothing written yet: the whole output buffer is free
        level_at = time.monotonic()
        while not stop.is_set():
            source.fill(ctypes.addressof(out_buf), block)
            ok = stream.write(ctypes.addressof(out_buf))
            written += block
            lags[(read + stream.read_available()) - (written - (size - stream.write_available()))] += 1
            ok = stream.read(ctypes.addressof(in_buf)) and ok
            read += block
            dropouts += not ok
            if not writer.write(bytes(in_buf)) or getattr(stream, "ended", False):
                break
            if clicks_end is not None and written >= clicks_end:
                break
            if time.monotonic() - level_at >= LEVEL_EVERY:
                level_at = time.monotonic()
                emit(event="level", frames=writer.frames, peak=writer.take_peak())
    except GoutError as exc:
        error = str(exc)
    finally:
        stream.close()
        source.close()
        writer.close()
    lag = lags.most_common(1)[0][0] if lags else 0
    emit(event="done", frames=writer.frames, rate=rate, lag=lag, lags=dict(lags.most_common(4)), dropouts=dropouts,
         gaps=source.gaps, top=writer.top, clipped=writer.clipped, full=writer.full, error=error)
    if error:
        emit(event="error", message=error)
    return 0
