"""Command dispatch and the entry point."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .core import __version__, BASE_COMMANDS, DB_NAME, die, GoutError, need_tools, use_bundled_tools
from .project import Project
from .helptext import help_text
from .fx import effects
from .commands import (
    Args,
    cmd_add,
    cmd_addons,
    cmd_cheat,
    cmd_colors,
    cmd_dump,
    cmd_fx,
    cmd_gain,
    cmd_import,
    cmd_ls,
    cmd_mix,
    cmd_move,
    cmd_mute,
    cmd_new,
    cmd_pan,
    cmd_play,
    cmd_record,
    cmd_calibrate,
    cmd_inputs,
    cmd_rebuild,
    cmd_rm,
    cmd_saveas,
    cmd_scan,
    cmd_set,
    cmd_solo,
    cmd_stats,
    cmd_stems,
    cmd_trim,
    cmd_ui,
    cmd_undo,
    cmd_video,
    cmd_view,
    effect_commands,
    print_kinds,
    run_tui,
)
from .cut import cmd_cut


BASE_PROJECT_COMMANDS = {
    "add": cmd_add, "scan": cmd_scan, "ls": cmd_ls, "move": cmd_move, "trim": cmd_trim, "rm": cmd_rm,
    "mute": cmd_mute, "solo": cmd_solo, "gain": cmd_gain, "pan": cmd_pan, "fx": cmd_fx,
    "set": cmd_set, "stats": cmd_stats, "mix": cmd_mix, "undo": cmd_undo, "dump": cmd_dump,
    "saveas": cmd_saveas, "stems": cmd_stems, "import": cmd_import,
    "view": cmd_view, "ui": cmd_ui, "play": cmd_play, "record": cmd_record, "video": cmd_video,
}


FREE_COMMANDS = {"new": cmd_new, "rebuild": cmd_rebuild, "cheat": cmd_cheat, "addons": cmd_addons,
                 "colors": cmd_colors, "inputs": cmd_inputs}


def command_table() -> dict[str, tuple[str, ...]]:
    """Long name -> short names, for every command including the effects'."""
    table = dict(BASE_COMMANDS)
    for eff in effects().values():
        table[eff.name] = eff.aliases
        for name in eff.shortcuts:
            table[name] = ()
    return table


def aliases() -> dict[str, str]:
    return {alias: name for name, short in command_table().items() for alias in short}


def project_commands() -> dict:
    return {**BASE_PROJECT_COMMANDS, **effect_commands()}


def run(argv: list[str], project: Project | None = None) -> int:
    """Run one command line. `project` is the open project when called from the UI."""
    root_hint: Path | None = None
    if "-p" in argv or "--project" in argv:
        i = argv.index("-p") if "-p" in argv else argv.index("--project")
        if i + 1 >= len(argv):
            die("-p needs a directory")
        root_hint = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
        if not root_hint.is_dir():
            die(f"no such directory: {root_hint}")

    if project is not None and root_hint is None:
        root_hint = project.root

    if not argv:
        found = project or Project.find(root_hint)
        if found is None:
            print(help_text(), end="")
            return 0
        need_tools()
        if sys.stdin.isatty() and sys.stdout.isatty():
            run_tui(found)
        else:
            cmd_ls(found, Args([]))
        return 0

    head, rest = argv[0], argv[1:]
    if head == "_engine":  # the process that plays and records at once (engine.py), not for people
        from .engine import child_main
        return child_main()
    head = aliases().get(head, head)
    if head in ("-h", "--help", "help"):
        print(help_text(), end="")
        return 0
    if head in ("-V", "--version", "version"):
        print(f"gout {__version__}")
        try:
            import curses  # noqa: F401  what the terminal ui needs; Windows has it from windows-curses
        except ImportError as exc:
            print(f"gout: the terminal ui cannot start here: {exc}"
                  + ("  (pip install windows-curses)" if os.name == "nt" else ""))
        from .engine import install_hint
        from .portaudio import available, version
        if not available():
            print(f"gout: PortAudio: not found, so gout record does not play the project while recording"
                  f" ({install_hint()})")
        return 0
    if head in ("quit", "clear", "split", "sheet", "stop") and project is None:
        die(f"{head} only means something inside the ui (gout, in a project)")

    need_tools()
    if head == "fx" and [w.lower() for w in rest] in (["kinds"], ["effects"]):
        print_kinds()  # listing the effects needs no project
        return 0
    if head == "record" and rest[:1] == ["calibrate"]:  # per computer, so no project needed
        cmd_calibrate(root_hint, Args(rest[1:]))
        return 0
    if head in FREE_COMMANDS:
        FREE_COMMANDS[head](root_hint, Args(rest))
        return 0
    table = project_commands()
    if head in table:
        found = project or Project.find(root_hint)
        if found is None:
            die(f"not inside a gout project (no {DB_NAME} here or above) — gout new NAME")
        table[head](found, Args(rest))
        found.sync_json()
        return 0
    if head == "cut":
        cmd_cut(rest)
        return 0
    if Path(head).is_file() or head.startswith("-"):
        cmd_cut(argv)  # the 1.x form: gout INPUT [-st ...]
        return 0
    die(f"unknown command {head!r}, and no such file — gout -h")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    use_bundled_tools()
    if os.name == "nt":  # a pipe or file on Windows is cp1252 otherwise, and gout prints ━ and ▶
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    try:
        return run(argv)
    except GoutError as exc:
        print(f"gout: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
