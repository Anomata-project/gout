"""The 1.x cutter: cut a piece out of a file by time or size, no project needed."""
from __future__ import annotations

import argparse
from pathlib import Path

from .core import __version__, die, fmt_size, fmt_time, parse_size, parse_time, TAG_ALLOWANCE
from .media import cut, cut_to_size, probe
from .helptext import help_text


class Parser(argparse.ArgumentParser):
    """Prints the hand-written instruction page instead of argparse's."""

    def format_help(self) -> str:
        return help_text()

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
