#!/usr/bin/env python3
"""Try an installed gout the way a person would: python3 packaging/smoke.py PATH-TO-GOUT

A project, two tracks, an effect, a mix with an mp3, playback (silent), stats, the example addons
and one of them in use. Stops at the first thing that goes wrong, printing what gout said."""
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


def tone(path: Path, freq: float, seconds: float = 3.0, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(int(seconds * rate))))


def main() -> None:
    gout = str(Path(sys.argv[1]).resolve()) if os.sep in sys.argv[1] or "/" in sys.argv[1] else shutil.which(sys.argv[1])
    work = Path(tempfile.mkdtemp(prefix="gout-smoke-"))
    env = dict(os.environ, GOUT_PLAYER="null", GOUT_ADDONS=str(work / "addons"), XDG_CONFIG_HOME=str(work / "config"))

    def step(*args: str, cwd: Path = work, expect: str = "") -> str:
        result = subprocess.run([gout, *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        out = result.stdout + result.stderr
        print(f"$ gout {' '.join(args)}\n{out.rstrip()}\n", flush=True)
        if result.returncode != 0 or expect not in out:
            sys.exit(f"smoke test failed at: gout {' '.join(args)}" + (f" (expected {expect!r})" if expect else ""))
        return out

    tone(work / "low.wav", 110)
    tone(work / "high.wav", 880, 2.0)
    version = step("version", expect="gout ")
    if "cannot start" in version:
        sys.exit("the terminal ui cannot start with this gout")
    step("new", "song")
    song = work / "song"
    step("add", str(work / "low.wav"), cwd=song, expect="add")
    step("add", str(work / "high.wav"), "--at", "1s", cwd=song, expect="add")
    step("eq", "2", "hp200", cwd=song)
    step("gain", "1", "-3", cwd=song)
    step("mix", "-3", cwd=song, expect="master.mp3")
    assert (song / "master.mp3").stat().st_size > 10000, "master.mp3 is too small"
    step("play", "0:01", cwd=song, expect="play")
    step("stats", cwd=song, expect="LUFS")
    step("view", "-w", "90", cwd=song, expect="master")
    step("addons", "examples", expect="fractal.py")
    step("addons", cwd=song, expect="fractal (screen")
    step("tremolo", "1", "slow", cwd=song, expect="tremolo")
    step("mix", cwd=song, expect="mix")
    step("undo", cwd=song, expect="undo")
    shutil.rmtree(work, ignore_errors=True)
    print("smoke test passed")


if __name__ == "__main__":
    main()
