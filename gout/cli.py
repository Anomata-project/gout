"""Command dispatch and the entry point."""
from __future__ import annotations

import sys
from pathlib import Path

from .core import __version__, BASE_COMMANDS, DB_NAME, die, GoutError, need_tools
from .project import Project
from .helptext import help_text
from .fx import effects
from .commands import (
    Args,
    cmd_add,
    cmd_cheat,
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
    cmd_view,
    effect_commands,
    run_tui,
)
from .cut import cmd_cut


BASE_PROJECT_COMMANDS = {
    "add": cmd_add, "scan": cmd_scan, "ls": cmd_ls, "move": cmd_move, "trim": cmd_trim, "rm": cmd_rm,
    "mute": cmd_mute, "solo": cmd_solo, "gain": cmd_gain, "pan": cmd_pan, "fx": cmd_fx,
    "set": cmd_set, "stats": cmd_stats, "mix": cmd_mix, "undo": cmd_undo, "dump": cmd_dump,
    "saveas": cmd_saveas, "stems": cmd_stems, "import": cmd_import,
    "view": cmd_view, "ui": cmd_ui,
}


FREE_COMMANDS = {"new": cmd_new, "rebuild": cmd_rebuild, "cheat": cmd_cheat}


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
    head = aliases().get(head, head)
    if head in ("-h", "--help", "help"):
        print(help_text(), end="")
        return 0
    if head in ("-V", "--version", "version"):
        print(f"gout {__version__}")
        return 0
    if head in ("quit", "clear", "split", "sheet") and project is None:
        die(f"{head} only means something inside the ui (gout, in a project)")

    need_tools()
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
    try:
        return run(argv)
    except GoutError as exc:
        print(f"gout: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
