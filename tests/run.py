"""The whole suite, with every test named as it goes and a watchdog on each one.

    python3 tests/run.py [--timeout SECONDS] [NAME ...]

A test that stops moving prints every thread's stack and ends the run, instead of hanging until
something else kills it (CI gave a hung mac job six hours). NAME picks what to run: a module
(test_play) or one test (test_play.PlayTest.test_it). Plain `python3 -m unittest discover -s tests`
still works and is quieter; this one is for CI and for hunting a hang.
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
    seconds, names = TIMEOUT, []
    while argv:
        if argv[0] in ("--timeout", "-t"):
            seconds, argv = float(argv[1]), argv[2:]
        else:
            names.append(argv.pop(0))
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(names) if names else loader.discover(str(HERE), top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=type("Watched", (Watched,), {"timeout": seconds}))
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
