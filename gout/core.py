"""Constants, errors, parsing and formatting of times and sizes, small helpers."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
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


progress_hook = None  # set by the ui while it runs a command: hook(what, fraction, status) -> True to stop it


def fmt_clock(seconds: float) -> str:
    """7:32, or 1:02:03 past an hour."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def bar(fraction: float, width: int = 28) -> str:
    """A bar in eighths of a character: ▕████▌      ▏"""
    eighths = round(max(0.0, min(1.0, fraction)) * width * 8)
    full, part = divmod(eighths, 8)
    inside = "█" * full + ("▏▎▍▌▋▊▉"[part - 1] if part else "")
    return "▕" + inside.ljust(width) + "▏"


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


PACKAGE_ROOT = Path(__file__).resolve().parents[1]  # the checkout, or where the package is installed


def frozen() -> bool:
    """True inside a packaged gout: the installers build one program with Python inside it."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Where the files that come with gout are (examples/, the licence): the checkout, or the
    unpacked program."""
    return Path(getattr(sys, "_MEIPASS", PACKAGE_ROOT)) if frozen() else PACKAGE_ROOT


def gout_command() -> list[str]:
    """The command that runs gout again in a new process, arguments to follow: the program itself
    when packaged (it has no python -c), otherwise this Python with this package on its path."""
    if frozen():
        return [sys.executable]
    return [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(PACKAGE_ROOT)!r}); "
                                  "from gout.cli import main; sys.exit(main(sys.argv[1:]))"]


def use_bundled_tools() -> None:
    """A packaged gout carries ffmpeg, ffprobe and ffplay in ffmpeg/ next to the program (macOS,
    Windows); they go first on PATH so every command finds them."""
    if not frozen():
        return
    folder = Path(sys.executable).resolve().parent / "ffmpeg"
    if folder.is_dir():
        os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def config_home() -> Path:
    """gout's own settings folder: $XDG_CONFIG_HOME/gout when set, %APPDATA%\\gout on Windows,
    otherwise ~/.config/gout (Linux and macOS)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base and os.name == "nt":
        base = os.environ.get("APPDATA")
    return Path(base or "~/.config").expanduser() / "gout"


def detached() -> dict:
    """Popen arguments for a process gout stops later with its children (a player, a render):
    its own session on Linux and macOS, its own process group on Windows, so ctrl-c in the
    terminal reaches gout and not them."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process(proc: subprocess.Popen) -> None:
    """Stop a process started with detached(), and whatever it started (a render's ffmpeg)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_quiet(cmd: list[str], verbose: bool = False) -> subprocess.CompletedProcess:
    if verbose:
        print("  $ " + " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"{cmd[0]} failed:\n{result.stderr.strip()}")
    return result


MASTER_N = 0  # the track number the master answers to in effect commands and the ui panel


MASTER_OWNER = "@master"  # whose effect chain: a track's file name, or this for the master bus


# commands other than effects: long name -> short names. Effects add their own names, and an
# addon may not take any of these.
BASE_COMMANDS = {
    "add": ("a",), "scan": ("sc",), "ls": ("l", "list"), "view": ("v",), "move": ("m", "mv"), "trim": ("t",),
    "rm": ("r", "remove", "del"), "mute": ("mu",), "solo": ("s",), "gain": ("g",), "pan": ("p",),
    "fx": ("f",), "mix": ("x", "render", "bounce"), "undo": ("u",), "dump": ("dp",), "rebuild": ("rb",),
    "set": ("se",), "stats": ("st",), "saveas": ("sa", "copy"), "stems": ("sm",), "import": ("im",),
    "new": ("n",), "cheat": ("c",), "help": ("h", "?"), "ui": ("tui",), "cut": (), "addons": (),
    "quit": ("q", "exit"), "clear": ("cl",), "split": ("sp",), "sheet": ("sh",), "version": ("-V",),
    "play": ("pl",), "stop": (), "colors": ("colours",), "record": ("rec", "rc"), "inputs": ("in",),
    "video": ("vd",), "part": ("pt",), "window": ("wd",), "loop": ("lo",),
}


def is_master(spec: str) -> bool:
    return spec.lower() in ("master", "m", str(MASTER_N))


IR_DIR = ".gout/ir"


def on_off(text: str | None, current: int) -> int:
    if text is None:
        return 0 if current else 1
    if text.lower() in ("on", "1", "yes", "true"):
        return 1
    if text.lower() in ("off", "0", "no", "false"):
        return 0
    die(f"expected on or off, got {text!r}")


STEMS_DIR = "stems"


CELL_AUDIBLE, CELL_SILENT, CELL_TRIMMED, CELL_ZERO, CELL_MASTER = "█", "▒", "░", "│", "━"


def pan_filter(channels: int, pan: float) -> str:
    """Stereo output, centre by default: mono goes equally to L and R."""
    left = min(1.0, 1.0 - pan)
    right = min(1.0, 1.0 + pan)
    if channels == 1:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0"
    if channels == 2:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"
    return f"aformat=channel_layouts=stereo,pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"
