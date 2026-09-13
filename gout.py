#!/usr/bin/env python3
"""gout — cut a piece out of an .mp3 by time or by target file size.

    gout song.mp3 -st 00:34:00 -fs 1.99      # 1.99 MB starting at 34:00
    gout song.mp3 -st 12 -el 3               # 3 minutes, starting 12 min in
    gout song.mp3 -st 00:01:30 -et 00:04:05  # absolute in/out points
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "1.0.0"

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


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"gout: {msg}", file=sys.stderr)
    sys.exit(1)


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
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h|hr)?", s)
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
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_size(nbytes: int) -> str:
    for unit, scale in (("GB", 1000**3), ("MB", 1000**2), ("kB", 1000)):
        if nbytes >= scale:
            return f"{nbytes / scale:.2f} {unit}"
    return f"{nbytes} B"


# --------------------------------------------------------------------------- ffmpeg


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bit_rate:format=duration,bit_rate",
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

    return {"duration": duration, "bitrate": bitrate, "codec": streams[0].get("codec_name")}


def cut(src: Path, dst: Path, start: float, duration: float | None,
        reencode: bool, bitrate: float, verbose: bool) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-map_metadata", "0", "-id3v2_version", "3", "-write_xing", "1"]
    if reencode:
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{max(32, round(bitrate / 1000))}k"]
    else:
        cmd += ["-c:a", "copy"]
    cmd.append(str(dst))

    if verbose:
        print("  $ " + " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"ffmpeg failed:\n{result.stderr.strip()}")


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


# --------------------------------------------------------------------------- cli

HELP = f"""\
gout {__version__} — cut a piece out of an .mp3, by time or by file size.

USAGE
  gout INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE] [flags]

WHERE THE CUT STARTS
  -st, --start TIME     measured from the beginning of the file (default 0)

WHERE THE CUT ENDS   (pick one; without any of them it runs to the end)
  -et, --end TIME       absolute end time, measured from the beginning
  -el, --length TIME    length of the cut, measured from the start
  -fs, --file-size SIZE cut as much audio as fits in this file size

OTHER FLAGS
  -o,  --output PATH    where to write it (default <name>_cut.mp3)
  -r,  --reencode       re-encode instead of copy: exact cut point, slower
  -f,  --force          overwrite the output file if it exists
  -n,  --dry-run        print what would be cut, write nothing
  -v,  --verbose        print the ffmpeg commands and each size pass
  -h,  --help           this page
  -V,  --version        print the version

TIME FORMATS
  00:34:00              HH:MM:SS
  00:34:00.500          HH:MM:SS.mmm      milliseconds after a dot
  00:34:00:500          HH:MM:SS:mmm      milliseconds after a fourth colon
  34:00                 MM:SS
  34                    a bare number is MINUTES, so -st 34 is 34 minutes in
  90s   2.5m   1.5h     explicit units (ms, s, m, h)

SIZE FORMATS
  1.99                  a bare number is MB
  700MB  1.99GB  500kB  decimal units, 1 MB = 1 000 000 bytes
  25MiB  1.99GiB        binary units,  1 MiB = 1 048 576 bytes
  MB/GB are decimal on purpose: -fs 1.99 stays under a 2 MB cap either way.

EXAMPLES
  gout show.mp3 -st 00:34:00 -fs 1.99      1.99 MB of audio starting at 34:00
  gout show.mp3 -st 12 -el 3               3 minutes, starting 12 minutes in
  gout show.mp3 -st 00:01:30 -et 00:04:05  absolute in and out points
  gout show.mp3 -el 00:00:30 -o intro.mp3  the first 30 seconds
  gout show.mp3 -st 45                     from 45:00 to the end of the file
  gout show.mp3 -st 45 -fs 24MB -n         estimate the length, write nothing

HOW -fs WORKS
  The length is estimated from the bitrate, cut, then measured and retried
  until the file lands between 97% and 100% of the limit — never over it.
  Constant-bitrate files take one pass, variable-bitrate ones a few; -v
  shows each pass.

NOTES
  Needs ffmpeg and ffprobe on PATH. The audio stream is copied, not
  re-encoded, so cutting is instant and lossless; that snaps the cut to an
  MP3 frame boundary (~26 ms). Use -r if you need the exact millisecond.
  Tags are carried over and a Xing header is written, so players report the
  right duration.
"""


class Parser(argparse.ArgumentParser):
    """Prints the hand-written instruction page instead of argparse's."""

    def format_help(self) -> str:
        return HELP

    def format_usage(self) -> str:
        return "usage: gout INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE]\n" \
               "       gout -h    for the full instructions\n"


def build_parser() -> argparse.ArgumentParser:
    p = Parser(
        prog="gout",
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


def main(argv: list[str] | None = None) -> int:
    if not (sys.argv[1:] if argv is None else argv):
        print(HELP, end="")  # bare `gout` shows the instructions
        return 0
    args = build_parser().parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} not found on PATH — install ffmpeg first")

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
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    if limit is not None:
        duration, size = cut_to_size(src, dst, start, limit, remaining,
                                     args.reencode, info["bitrate"], args.verbose)
        print(f"      {fmt_time(start)} -> {fmt_time(start + duration)} "
              f"({fmt_time(duration)}, {size / limit:.1%} of the limit)")
    else:
        cut(src, dst, start, duration, args.reencode, info["bitrate"], args.verbose)
        size = dst.stat().st_size

    print(f"out   {dst}  ({fmt_size(size)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
