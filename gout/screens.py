"""Screens: full-screen views an addon adds to the ui, opened with a key or by name, closed with esc.

A screen draws text. The ui calls `frame(ctx, width, height)` about `fps` times a second while
the song plays, and draws the rows it returns under a status line. Space plays and stops, the
left and right arrows move 5 s, esc (or the screen's key again) goes back to gout. Other keys go
to `key_pressed`.

    from gout.screens import Screen

    class Bars(Screen):
        name = "bars"
        key = "ctrl-b"
        summary = "the three bands as bars"

        def frame(self, ctx, width, height):
            rows = []
            for band in ("low", "mid", "high"):
                n = round(ctx.band(band) * width)
                rows.append(("#" * n, "0" * n))
            return rows

    def register(gout):
        gout.add_screen(Bars())

A row is (text, classes): one class character per character of text. Digits are the track
colours from color.json, "m" the master's, a space the terminal's own.
"""
from __future__ import annotations

import re
from pathlib import Path

KEY_CODES = {  # keys a screen may take: nothing else in the ui uses them
    "ctrl-space": "\x00", "ctrl-b": "\x02", "ctrl-o": "\x0f", "ctrl-q": "\x11",
    "ctrl-v": "\x16", "ctrl-y": "\x19",  # ctrl-r records
}
UI_WORDS = {"quit", "q", "exit", "clear", "cl", "split", "sp", "sheet", "sh", "view", "timeline", "help",
            "cheat", "play", "stop", "saveas", "ui", "tui"}
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}")


class Screen:
    """One full-screen view. Subclass it, fill in the attributes, implement frame()."""

    name = ""                       # typed at the prompt to open it
    aliases: tuple[str, ...] = ()   # short names
    key = ""                        # one of KEY_CODES, or "" for none
    summary = ""                    # one line for the cheat sheet and gout addons
    fps = 20                        # frames a second while playing
    help: tuple[tuple[str, str], ...] = ()   # (keys, what they do) for the cheat sheet
    play_on_open = True             # start the song when the screen opens
    fullscreen = False              # ask the terminal window for fullscreen while open (GNOME Terminal)
    status_seconds = 3.0            # the status line hides this long after opening or a key; 0 keeps it
    source = "built-in"             # or the addon file it came from

    def command(self, ctx: "ScreenContext", words: list[str]) -> tuple[list[str], bool]:
        """Called on every opening, with what was typed after the name (nothing for the key).
        Returns lines for the log and whether to open. Raise ValueError to refuse with a message."""
        if words:
            return [f"{self.name} takes nothing after its name"], False
        return [], True

    def frame(self, ctx: "ScreenContext", width: int, height: int) -> list[tuple[str, str]]:
        """`height` rows of (text, classes), each `width` characters (shorter rows are padded)."""
        raise NotImplementedError

    def status(self, ctx: "ScreenContext") -> str:
        """The left part of the status line."""
        return self.name

    def key_pressed(self, ctx: "ScreenContext", key: str) -> bool:
        """A key the ui did not use: a character, or up down pgup pgdn home end enter backspace
        tab. True when the screen used it."""
        return False

    def choices(self) -> list[str]:
        """What the screen can switch between, by name: gout video NAME all goes through them in turn.
        None by default."""
        return []

    def pick(self, ctx: "ScreenContext", word: str) -> None:
        """Switch to one of choices(), or anything command() takes, for a video: without remembering it
        as the user's choice. Raise ValueError when it is not one. By default: command(ctx, [word])."""
        lines, ok = self.command(ctx, [word])
        if not ok:
            raise ValueError("; ".join(lines) or f"{self.name} cannot show {word!r}")


class ScreenContext:
    """What a screen may know: where the song is and how it sounds there."""

    def __init__(self, project):
        self.project = project
        self.position_ms = 0.0       # project time of what is heard now
        self.playing = False
        self.length_ms = 0
        self.features = None         # analysis.Features once the song has been listened to
        self.note = ""               # shown in the status line: listening, or what went wrong
        self.offline = False         # True while a video is drawn: take the time needed, the same moment gives the same picture
        self.choice_ms = 0.0         # while a video is drawn: the project time the current choice began showing

    def band(self, name: str, window_ms: float = 100) -> float:
        """low, mid, high, level or onset: 0 .. 1, averaged over the last window_ms."""
        if self.features is None:
            return 0.0
        return self.features.mean(name, self.position_ms, window_ms)

    def travel(self, name: str) -> float:
        """How much of the band has gone by up to now, in seconds at full level: it only grows,
        faster when the band is busy. Good for motion that should push with the music."""
        if self.features is None:
            return 0.0
        return self.features.total(name, self.position_ms)


def config_file(name: str) -> Path:
    """A settings file for a screen, next to color.json: ~/.config/gout/NAME (%APPDATA%\\gout on Windows)."""
    from .core import config_home
    return config_home() / name


_SCREENS: dict[str, Screen] = {}


def taken_words() -> set[str]:
    words = set()
    for s in _SCREENS.values():
        words |= {s.name, *s.aliases}
    return words


def register_screen(screen: Screen, source: str = "built-in") -> None:
    """Add a screen. ValueError when its name, a short name or its key is taken or malformed."""
    from .fx import taken_names
    if not isinstance(screen, Screen):
        raise ValueError(f"{source}: add_screen() wants a Screen instance, got {type(screen).__name__}")
    words = (screen.name, *screen.aliases)
    for word in words:
        if not NAME_RE.fullmatch(word):
            raise ValueError(f"{source}: {word!r} is not a usable name (lowercase letters, digits, - or _)")
        if word in taken_names() or word in UI_WORDS:
            raise ValueError(f"{source}: the name {word!r} is already taken")
    if screen.key:
        if screen.key not in KEY_CODES:
            raise ValueError(f"{source}: key {screen.key!r} is not free for screens; use one of {', '.join(KEY_CODES)}")
        other = next((s for s in _SCREENS.values() if s.key == screen.key), None)
        if other is not None:
            raise ValueError(f"{source}: {screen.key} already opens the {other.name} screen")
    screen.source = source
    _SCREENS[screen.name] = screen


def screens() -> dict[str, Screen]:
    """Every screen there is (addons load with the effects)."""
    from .fx import effects
    effects()
    return _SCREENS


def screen_named(word: str) -> Screen | None:
    word = word.lower()
    return next((s for s in screens().values() if word == s.name or word in s.aliases), None)


def screen_for_key(key) -> Screen | None:
    if not isinstance(key, str):
        return None
    return next((s for s in screens().values() if s.key and KEY_CODES[s.key] == key), None)
