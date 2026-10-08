"""The suite, with every test named as it goes and a watchdog on each one.

    python3 tests/run.py [--timeout SECONDS] [NAME ...]     everything, or what NAME picks
    python3 tests/run.py --changed                          only what the changes at hand can break

A test that stops moving prints every thread's stack and ends the run, instead of hanging until
something else kills it (CI gave a hung mac job six hours). NAME picks what to run: a module
(test_play) or one test (test_play.PlayTest.test_it). Plain `python3 -m unittest discover -s tests`
still works and is quieter; this one is for CI and for hunting a hang.

--changed looks at the files that differ from what was last pushed (edited, new, or committed and
not pushed yet) and runs the test files that cover them, by the table SCOPE below. The whole
suite is a quarter of an hour of work; CI runs it on every push. Before a commit, what the change
can break is enough: a change in bonepipe/ is 20 tests and 7 seconds, not 262 and 14 minutes.
A change to something everything rests on (the project store, the mixer, the commands, the ui)
still runs everything, and so does a file the table does not know.
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 300.0  # seconds one test may take; the slowest real one is well under a minute

EVERYTHING = ["*"]
NOTHING: list[str] = []
INSTRUMENTS = ["test_instruments"]
PICTURES = ["test_vtrack", "test_sync", "test_vrender", "test_video", "test_viewer"]
# what a changed file can break: the first start of a path that matches decides
SCOPE: list[tuple[str, list[str]]] = [
    ("bonepipe/tools/", ["test_bonepipe"]),
    ("bonepipe/", ["test_bonepipe", "test_instruments"]),         # the bone pipe instrument plays what it computes
    ("gout/instruments/", INSTRUMENTS), ("gout/inst.py", INSTRUMENTS), ("gout/instcmd.py", INSTRUMENTS),
    ("gout/pattern.py", INSTRUMENTS), ("gout/synthkit.py", INSTRUMENTS), ("gout/grid.py", INSTRUMENTS),
    ("gout/sequencer.py", INSTRUMENTS + ["test_mix", "test_project"]),   # it runs before every mix and command
    ("gout/effects/", ["test_fx", "test_chain", "test_addons", "test_mix"]),
    ("gout/fx.py", ["test_fx", "test_chain", "test_addons", "test_mix", "test_instruments", "test_screens"]),
    ("gout/addons.py", ["test_addons", "test_screens", "test_instruments"]),
    ("examples/addons/", ["test_addons", "test_screens", "test_video"]),
    ("gout/screens.py", ["test_screens", "test_addons", "test_video", "test_instruments"]),
    ("gout/formula.py", ["test_formula", "test_screens"]),
    ("gout/analysis.py", ["test_screens", "test_video", "test_sync"]),
    ("gout/video.py", ["test_video", "test_vrender"]), ("gout/vrender.py", ["test_vrender", "test_video"]),
    ("gout/vthumbs.py", ["test_viewer"]), ("gout/motion.py", ["test_sync"]), ("gout/timemap.py", ["test_sync"]),
    ("gout/sync.py", ["test_sync", "test_vtrack", "test_vrender"]),
    ("gout/viewer.py", ["test_viewer"]), ("gout/viewerpage/", ["test_viewer"]), ("gout/window.py", ["test_screens"]),
    ("gout/recorder.py", ["test_record"]), ("gout/engine.py", ["test_record"]), ("gout/portaudio.py", ["test_record"]),
    ("gout/player.py", ["test_play", "test_live", "test_loop", "test_ui"]),
    ("gout/lineedit.py", ["test_ui"]), ("gout/theme.py", ["test_timeline", "test_ui"]),
    ("gout/render.py", ["test_timeline", "test_ui", "test_chain", "test_addons", "test_instruments"]),
    ("gout/helptext.py", ["test_ui", "test_addons", "test_instruments"]),
    ("gout/cut.py", ["test_cut"]),
    ("gout/media.py", ["test_cut", "test_mix", "test_project", "test_timeline", "test_record"] + PICTURES),
    ("gout/", EVERYTHING),          # the project store, the mixer, the commands, the ui, and whatever is new
    ("bin/", EVERYTHING), ("tests/helpers.py", EVERYTHING),
    ("tests/run.py", NOTHING),
    ("tests/test_ui.py", EVERYTHING),   # its fake screen is what the other ui tests draw on
    ("tests/", ["="]),                  # a test file: itself
]


def scope(paths: list[str]) -> tuple[list[str], list[str]]:
    """The test files that cover these changed paths (["*"] for all of them), and why: a line a path."""
    picked: list[str] = []
    why: list[str] = []
    for path in paths:
        tests = next((tests for start, tests in SCOPE if path.startswith(start)), NOTHING)   # docs, notes, packaging: nothing
        if tests == ["="]:
            tests = [Path(path).stem] if Path(path).name.startswith("test_") else NOTHING
        why.append(f"{path}: {'everything' if tests == EVERYTHING else ' '.join(tests) or 'no tests'}")
        picked += [t for t in tests if t not in picked]
    if "*" in picked:
        return EVERYTHING, why
    return sorted(picked), why


def changed_paths() -> list[str]:
    """Files edited, added or deleted since the last push: the working tree, and commits not pushed yet."""
    import subprocess

    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=HERE.parent, capture_output=True, text=True, encoding="utf-8")
        return [line for line in out.stdout.splitlines() if line.strip()] if out.returncode == 0 else []

    found = git("diff", "--name-only", "HEAD") + git("ls-files", "--others", "--exclude-standard")
    found += git("diff", "--name-only", "@{upstream}..HEAD")
    return sorted(set(found))

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class Watched(unittest.TextTestResult):
    """Names each test before it runs and keeps a timer on it."""

    timeout = TIMEOUT

    def startTest(self, test: unittest.TestCase) -> None:
        self.timer = threading.Timer(self.timeout, self.hung, [test])
        self.timer.daemon = True
        self.timer.start()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.timer.cancel()
        super().stopTest(test)

    def hung(self, test: unittest.TestCase) -> None:
        print(f"\n\nhung: {test.id()} has not moved for {self.timeout:.0f} s."
              f"  Every thread, as it stands:\n", file=sys.stderr, flush=True)
        faulthandler.dump_traceback()
        sys.stderr.flush()
        os._exit(3)  # a hung test holds threads and children that a clean exit would wait for


def main(argv: list[str]) -> int:
    if os.name == "nt":  # a failure message full of braille waves would not fit cp1252, like gout's own output
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    seconds, names, by_change = TIMEOUT, [], False
    while argv:
        if argv[0] in ("--timeout", "-t"):
            seconds, argv = float(argv[1]), argv[2:]
        elif argv[0] in ("--changed", "-c"):
            by_change, argv = True, argv[1:]
        else:
            names.append(argv.pop(0))
    if by_change:
        names, why = scope(changed_paths())
        print("\n".join(why) or "nothing differs from what was pushed", flush=True)
        if not names:
            print("no tests cover that: nothing to run", flush=True)
            return 0
        print("running " + ("everything" if names == EVERYTHING else " ".join(names)) + "\n", flush=True)
        names = [] if names == EVERYTHING else [n for n in names if (HERE / f"{n}.py").is_file()]
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(names) if names else loader.discover(str(HERE), top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=type("Watched", (Watched,), {"timeout": seconds}))
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
