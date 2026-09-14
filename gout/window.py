"""Fullscreen for the terminal window gout runs in, where the terminal can be asked.

GNOME Terminal exports each window's actions on the session bus (enter-fullscreen,
leave-fullscreen, and a fullscreen state), and puts its bus name in $GNOME_TERMINAL_SERVICE for
the programs it runs. It does not say which window a program runs in, so gout asks the newest
window first and watches its own terminal: if it grew, that was the window; if not, the window is
put back and the next one is tried. Once found, the window is remembered for the session.
A window that is already fullscreen is never touched, and nothing is tried when one is, since it
may well be gout's own. Other terminals: nothing happens.

Calls go through the gdbus tool (part of GLib), so the core stays stdlib-only.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

WINDOWS = "/org/gnome/Terminal/window"


def gdbus(args: list[str]) -> str:
    result = subprocess.run(["gdbus", *args], capture_output=True, text=True, timeout=3)
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"gdbus exited {result.returncode}")
    return result.stdout


def terminal_size() -> tuple[int, int] | None:
    for stream in (sys.__stdout__, sys.__stdin__):
        try:
            size = os.get_terminal_size(stream.fileno())
            return size.columns, size.lines
        except (OSError, ValueError, AttributeError):
            continue
    return None


class TerminalWindow:
    def __init__(self, env=None, run=None, size=None, sleep=time.sleep, wait_s: float = 1.0):
        env = os.environ if env is None else env
        self.service = env.get("GNOME_TERMINAL_SERVICE", "")
        self.run = run
        if self.run is None and self.service and shutil.which("gdbus"):
            self.run = gdbus
        self.size = size or terminal_size
        self.sleep = sleep
        self.wait_s = wait_s
        self.entered: str | None = None   # the window gout made fullscreen, to put back
        self.known: str | None = None     # gout's own window, once found

    def available(self) -> bool:
        return bool(self.service and self.run)

    def call(self, path: str, method: str, *args: str) -> str:
        return self.run(["call", "--session", "--dest", self.service, "--object-path", path,
                         "--method", f"org.gtk.Actions.{method}", *args])

    def windows(self) -> list[str]:
        """Window object paths, newest first."""
        text = self.run(["introspect", "--session", "--dest", self.service, "--object-path", WINDOWS])
        numbers = sorted({int(n) for n in re.findall(r"node (\d+)", text)}, reverse=True)
        return [f"{WINDOWS}/{n}" for n in numbers]

    def is_fullscreen(self, path: str) -> bool | None:
        match = re.search(r"\[<(true|false)>\]", self.call(path, "Describe", "fullscreen"))
        return None if match is None else match.group(1) == "true"

    def activate(self, path: str, action: str) -> None:
        self.call(path, "Activate", action, "[]", "{}")

    def grew(self, before: tuple[int, int]) -> bool:
        waited = 0.0
        while waited < self.wait_s:
            now = self.size()
            if now is not None and now[0] * now[1] > before[0] * before[1]:
                return True
            self.sleep(0.03)
            waited += 0.03
        return False

    def enter(self) -> str:
        """Make gout's window fullscreen. '' when done or not possible here, else a note why not."""
        if not self.available() or self.entered:
            return ""
        before = self.size()
        if before is None:
            return ""
        try:
            windows = self.windows()
            states = {path: self.is_fullscreen(path) for path in windows}
            if self.known in states:
                candidates = [self.known]
            elif any(states.values()):
                return ""  # a fullscreen window may be this one: leave everything as it is
            else:
                candidates = windows
            for path in candidates:
                if states.get(path) is not False:
                    continue
                self.activate(path, "enter-fullscreen")
                if self.grew(before):
                    self.entered = self.known = path
                    return ""
                self.activate(path, "leave-fullscreen")
        except (OSError, subprocess.SubprocessError) as exc:
            return f"fullscreen: {exc}"
        return "fullscreen: could not tell which terminal window this is"

    def leave(self) -> None:
        if not self.entered:
            return
        path, self.entered = self.entered, None
        try:
            self.activate(path, "leave-fullscreen")
        except (OSError, subprocess.SubprocessError):
            pass
