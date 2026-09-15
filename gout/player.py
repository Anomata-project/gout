"""Playing audio through whatever this machine has.

ffplay (part of most ffmpeg installs) is used when it is there. Otherwise ffmpeg decodes to raw
16-bit stereo and pipes it into pw-cat (PipeWire), paplay (PulseAudio) or aplay (ALSA). The
GOUT_PLAYER environment variable picks one of those by name; GOUT_PLAYER=null "plays" through
ffmpeg -re into nothing, in real time and in silence, which is what the tests use, and
GOUT_PLAYER=file:/some/path.wav writes what would be heard to a file, for checking it.

The position is wall-clock time since the player started plus where it started, which is what a
playhead needs; players start within a fraction of a second.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .core import detached, die, stop_process

BACKENDS = ("ffplay", "pw-cat", "paplay", "aplay")
RATE = 48000
# seconds from starting a player to hearing it, roughly: measured about 0.3 s overall on PipeWire
STARTUP = {"ffplay": 0.25, "pw-cat": 0.2, "paplay": 0.25, "aplay": 0.2, "null": 0.0}


def choose_backend() -> str:
    wanted = os.environ.get("GOUT_PLAYER", "").strip()
    if wanted:
        if wanted == "null" or wanted.startswith("file:") or (wanted in BACKENDS and shutil.which(wanted)):
            return wanted
        die(f"GOUT_PLAYER={wanted}: use one of {', '.join(BACKENDS)} or null, and it must be installed")
    for name in BACKENDS:
        if shutil.which(name):
            return name
    die("nothing to play audio with: install ffplay (it comes with most ffmpeg packages), "
        "or pipewire, pulseaudio or alsa-utils")


class Player:
    """Plays a file from a position, or a stream: ffmpeg input and filter arguments (no output)
    that produce the audio, as live playback of an unrendered project does."""

    def __init__(self, path: Path | None, start_s: float, length_s: float, stream: list[str] | None = None,
                 loop: tuple[float, float] | None = None, stream_start: float = 0.0):
        """loop: (from, to) in the same seconds as start_s: play from start_s to `to`, then from `from` to
        `to` over and over, inside ffmpeg (aloop), so the turn has no gap. stream_start: where a
        stream's own time 0 is."""
        self.path, self.start_s, self.length_s = path, max(0.0, start_s), length_s
        self.stream = stream
        self.loop, self.stream_start = loop, stream_start
        self.procs: list[subprocess.Popen] = []
        self.backend = ""
        self.t0 = 0.0

    @property
    def live(self) -> bool:
        return self.stream is not None

    def source_args(self, realtime: bool = False) -> list[str]:
        """ffmpeg arguments up to the output: the file from start_s, or the stream."""
        if self.loop is not None:
            return self.looped_args(realtime)
        if self.stream is None:
            return (["-re"] if realtime else []) + ["-ss", f"{self.start_s:.3f}", "-i", str(self.path)]
        if not realtime:
            return list(self.stream)
        out = list(self.stream)  # throttle the output, not the inputs: what is trimmed off comes at once
        graph, label = out.index("-filter_complex") + 1, out.index("-map") + 1
        out[graph] += f";{out[label]}arealtime[gout_realtime]"
        out[label] = "[gout_realtime]"
        return out

    def looped_args(self, realtime: bool) -> list[str]:
        """The first stretch to the loop's end, then the loop again and again. Measured: clicks in a
        0.5 s loop come back every 0.500 s, from a file and from a filter graph alike."""
        low, high = self.loop
        span = high - low
        if self.stream is None:
            inputs, graph, source, base = ["-i", str(self.path)], "", "[0:a]", 0.0
        else:
            at = self.stream.index("-filter_complex")
            inputs, graph, source = self.stream[:at], self.stream[at + 1] + ";", self.stream[at + 3]
            base = self.stream_start
        first, again = self.start_s - base, low - base
        graph += (f"{source}aresample={RATE},asplit=2[gout_la][gout_lb];"
                  f"[gout_la]atrim=start={first:.6f}:end={again + span:.6f},asetpts=PTS-STARTPTS[gout_first];"
                  f"[gout_lb]atrim=start={again:.6f}:end={again + span:.6f},asetpts=PTS-STARTPTS,"
                  f"aloop=loop=-1:size={round(span * RATE)}[gout_again];"
                  f"[gout_first][gout_again]concat=n=2:v=0:a=1" + (",arealtime" if realtime else "") + "[gout_loop]")
        return [*inputs, "-filter_complex", graph, "-map", "[gout_loop]"]

    def start(self) -> "Player":
        self.backend = choose_backend()
        quiet = {"stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, **detached()}
        ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin"]
        raw = ["-f", "s16le", "-ac", "2", "-ar", str(RATE), "-"]
        if self.backend.startswith("file:"):  # for tests: write what would be heard, as fast as possible
            enough = ["-t", f"{self.loop[1] - self.start_s + 2 * (self.loop[1] - self.loop[0]):.3f}"] if self.loop else []
            self.procs = [subprocess.Popen([*ffmpeg, "-y", *self.source_args(), *enough, "-ac", "2", "-ar", str(RATE),
                                            "-c:a", "pcm_s16le", self.backend[5:]], stdout=subprocess.DEVNULL, **quiet)]
        elif self.backend == "null":
            self.procs = [subprocess.Popen([*ffmpeg, *self.source_args(realtime=True), "-f", "null", "-"],
                                           stdout=subprocess.DEVNULL, **quiet)]
        elif self.backend == "ffplay" and self.stream is None and self.loop is None:
            self.procs = [subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                                            "-ss", f"{self.start_s:.3f}", str(self.path)], stdout=subprocess.DEVNULL, **quiet)]
        else:
            decoder = subprocess.Popen([*ffmpeg, *self.source_args(), *raw], stdout=subprocess.PIPE, **quiet)
            sink = {
                "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "s16le",
                           "-sample_rate", str(RATE), "-ch_layout", "stereo", "-i", "-"],
                "pw-cat": ["pw-cat", "--playback", "--format", "s16", "--rate", str(RATE), "--channels", "2", "-"],
                "paplay": ["paplay", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=2"],
                "aplay": ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", str(RATE)],
            }[self.backend]
            player = subprocess.Popen(sink, stdin=decoder.stdout, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, **detached())
            decoder.stdout.close()  # the sink owns the pipe now
            self.procs = [decoder, player]
        self.t0 = time.monotonic()
        return self

    def position(self) -> float:
        """Seconds into the file, allowing for the time the player takes to start sounding."""
        elapsed = max(0.0, time.monotonic() - self.t0 - STARTUP.get(self.backend, 0.0))
        if self.loop is not None:
            low, high = self.loop
            first = high - self.start_s
            return self.start_s + elapsed if elapsed < first else low + (elapsed - first) % (high - low)
        return min(self.length_s, self.start_s + elapsed)

    def running(self) -> bool:
        if not self.procs:
            return False
        if self.procs[-1].poll() is None:
            return True
        for proc in self.procs[:-1]:  # the sink is done; do not leave the decoder behind
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return False

    def stop(self) -> None:
        for proc in self.procs:
            stop_process(proc)
        for proc in self.procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
