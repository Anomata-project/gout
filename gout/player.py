"""Playing audio through whatever this machine has.

ffplay (part of most ffmpeg installs) is used when it is there. Otherwise ffmpeg decodes to raw
16-bit stereo and pipes it into pw-cat (PipeWire), paplay (PulseAudio) or aplay (ALSA). The
GOUT_PLAYER environment variable picks one of those by name; GOUT_PLAYER=null "plays" through
ffmpeg -re into nothing, in real time and in silence, which is what the tests use.

The position is wall-clock time since the player started plus where it started, which is what a
playhead needs; players start within a fraction of a second.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .core import die

BACKENDS = ("ffplay", "pw-cat", "paplay", "aplay")
RATE = 48000
# seconds from starting a player to hearing it, roughly: measured about 0.3 s overall on PipeWire
STARTUP = {"ffplay": 0.25, "pw-cat": 0.2, "paplay": 0.25, "aplay": 0.2, "null": 0.0}


def choose_backend() -> str:
    wanted = os.environ.get("GOUT_PLAYER", "").strip()
    if wanted:
        if wanted == "null" or (wanted in BACKENDS and shutil.which(wanted)):
            return wanted
        die(f"GOUT_PLAYER={wanted}: use one of {', '.join(BACKENDS)} or null, and it must be installed")
    for name in BACKENDS:
        if shutil.which(name):
            return name
    die("nothing to play audio with: install ffplay (it comes with most ffmpeg packages), "
        "or pipewire, pulseaudio or alsa-utils")


class Player:
    def __init__(self, path: Path, start_s: float, length_s: float):
        self.path, self.start_s, self.length_s = path, max(0.0, start_s), length_s
        self.procs: list[subprocess.Popen] = []
        self.backend = ""
        self.t0 = 0.0

    def start(self) -> "Player":
        self.backend = choose_backend()
        quiet = {"stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True}
        seek = ["-ss", f"{self.start_s:.3f}"]
        if self.backend == "ffplay":
            self.procs = [subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", *seek,
                                            str(self.path)], stdout=subprocess.DEVNULL, **quiet)]
        elif self.backend == "null":
            self.procs = [subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-re", *seek,
                                            "-i", str(self.path), "-f", "null", "-"], stdout=subprocess.DEVNULL, **quiet)]
        else:
            decoder = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "quiet", *seek, "-i", str(self.path),
                                        "-f", "s16le", "-ac", "2", "-ar", str(RATE), "-"], stdout=subprocess.PIPE, **quiet)
            sink = {
                "pw-cat": ["pw-cat", "--playback", "--format", "s16", "--rate", str(RATE), "--channels", "2", "-"],
                "paplay": ["paplay", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=2"],
                "aplay": ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", str(RATE)],
            }[self.backend]
            player = subprocess.Popen(sink, stdin=decoder.stdout, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
            decoder.stdout.close()  # the sink owns the pipe now
            self.procs = [decoder, player]
        self.t0 = time.monotonic()
        return self

    def position(self) -> float:
        """Seconds into the file, allowing for the time the player takes to start sounding."""
        elapsed = time.monotonic() - self.t0 - STARTUP.get(self.backend, 0.0)
        return min(self.length_s, self.start_s + max(0.0, elapsed))

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
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        for proc in self.procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
