"""Command dispatch and the entry point."""
from __future__ import annotations

import sys
from pathlib import Path

from .core import __version__, DB_NAME, die, GoutError, need_tools
from .project import Project
from .helptext import HELP
from .commands import (
    Args,
    cmd_add,
    cmd_cheat,
    cmd_comp,
    cmd_delay,
    cmd_dump,
    cmd_eq,
    cmd_gain,
    cmd_hp,
    cmd_import,
    cmd_lp,
    cmd_ls,
    cmd_mix,
    cmd_move,
    cmd_mute,
    cmd_new,
    cmd_pan,
    cmd_rebuild,
    cmd_reverb,
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
)
from .cut import cmd_cut
from .commands import run_tui


# long name -> the short form and the other spellings; every command works under all of them
COMMANDS = {
    "add": ("a",), "scan": ("sc",), "ls": ("l", "list"), "view": ("v",), "move": ("m", "mv"), "trim": ("t",),
    "rm": ("r", "remove", "del"), "mute": ("mu",), "solo": ("s",), "gain": ("g",), "pan": ("p",), "eq": ("e",), "hp": (), "lp": (), "comp": ("cp",), "delay": ("dl", "echo"), "reverb": ("rv", "verb"),
    "mix": ("x", "render", "bounce"), "undo": ("u",), "dump": ("dp",), "rebuild": ("rb",),
    "set": ("se",), "stats": ("st",), "saveas": ("sa", "copy"), "stems": ("sm",), "import": ("im",), "new": ("n",), "cheat": ("c",), "help": ("h", "?"), "ui": ("tui",), "cut": (),
    "quit": ("q", "exit"), "clear": ("cl",), "split": ("sp",), "sheet": ("sh",),
}


ALIASES = {alias: name for name, aliases in COMMANDS.items() for alias in aliases}


PROJECT_COMMANDS = {
    "add": cmd_add, "scan": cmd_scan, "ls": cmd_ls, "move": cmd_move, "trim": cmd_trim, "rm": cmd_rm,
    "mute": cmd_mute, "solo": cmd_solo, "gain": cmd_gain, "pan": cmd_pan,
    "eq": cmd_eq, "hp": cmd_hp, "lp": cmd_lp, "comp": cmd_comp, "delay": cmd_delay, "reverb": cmd_reverb,
    "set": cmd_set, "stats": cmd_stats, "mix": cmd_mix, "undo": cmd_undo, "dump": cmd_dump,
    "saveas": cmd_saveas, "stems": cmd_stems, "import": cmd_import,
    "view": cmd_view, "ui": cmd_ui,
}


FREE_COMMANDS = {"new": cmd_new, "rebuild": cmd_rebuild, "cheat": cmd_cheat}


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
            print(HELP, end="")
            return 0
        need_tools()
        if sys.stdin.isatty() and sys.stdout.isatty():
            run_tui(found)
        else:
            cmd_ls(found, Args([]))
        return 0

    head, rest = argv[0], argv[1:]
    head = ALIASES.get(head, head)
    if head in ("-h", "--help", "help"):
        print(HELP, end="")
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
    if head in PROJECT_COMMANDS:
        found = project or Project.find(root_hint)
        if found is None:
            die(f"not inside a gout project (no {DB_NAME} here or above) — gout new NAME")
        PROJECT_COMMANDS[head](found, Args(rest))
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
