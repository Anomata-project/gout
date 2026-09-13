"""Effects: the class every effect implements, the context it runs in, and the registry.

An effect is one settings line (``hp80 +3@200``, ``-18 4:1``, ``hall w20``) that turns into
ffmpeg filters. Built-in effects live in gout/effects/; addons follow exactly the same shape
and are loaded from the user's addon folder (see gout/addons.py). Everything the commands,
the parameter sheet, gout.json, help and the cheat sheet show about an effect comes from here,
so adding one means writing one class.
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import BASE_COMMANDS, MASTER_OWNER, MASTER_WAV

GUTTER = 4  # label columns on the left of every effect picture

Rows = list  # [(text, classes, kind)]: kind is head, graph or axis; classes mark each character


class Effect:
    """One kind of effect. Subclass it, fill in the attributes, override what you need."""

    name = ""                                   # command name, and the kind stored in a chain
    aliases: tuple[str, ...] = ()               # short names: ("e",)
    summary = ""                                # one line for help and gout fx kinds
    syntax = ""                                 # an example settings line
    hint = ""                                   # short hint in the parameter sheet
    presets: dict[str, tuple[str, str]] = {}    # name -> (settings line, what it is for)
    empty = "(defaults)"                        # how an empty settings line reads
    order = 50                                  # where `gout NAME TRACK ...` inserts a new one
    picture_width = (40, 80)                    # smallest and largest useful picture width
    picture_height = 8
    legend = ""                                 # printed under the picture by `gout NAME TRACK`
    cheat: tuple[str, ...] = ()                 # cheat-sheet lines, 60 columns at most
    help: tuple[str, ...] = ()                  # extra lines for gout help
    # extra commands that edit this effect's line: name -> (usage, summary, fn(line, words) -> line)
    shortcuts: dict = {}
    source = "built-in"                         # or the addon file it came from

    # ---- what an effect implements

    def parse(self, text: str) -> dict:
        """Settings from a line without presets. Raise ValueError with a readable message."""
        raise NotImplementedError

    def format(self, params: dict) -> str:
        """The canonical line for these settings; parse(format(p)) must give p back."""
        raise NotImplementedError

    def check(self, ctx: "FxContext", params: dict) -> None:
        """Raise GoutError when the project cannot run these settings (a delay in 1/8 needs a bpm)."""

    def filters(self, ctx: "FxContext", params: dict) -> list[str]:
        """ffmpeg audio filters for a stereo signal, applied in order."""
        return []

    def graph(self, ctx: "FxContext", params: dict, src: str, out: str, inputs: list[Path]) -> str:
        """For effects a filter list cannot express: a filtergraph from the stereo label `src` to
        [out]. Extra files (an impulse response) are appended to `inputs` and used as
        [len(inputs)-1:a]. Labels you make must start with `out`. Only called when overridden."""
        raise NotImplementedError

    def tail_ms(self, ctx: "FxContext", params: dict) -> int:
        """How long the effect keeps sounding after its input stops."""
        return 0

    def picture(self, ctx: "FxContext", params: dict | None, width: int, height: int) -> Rows | None:
        """A text picture: a head row, `height` graph rows, an axis row. params is None when the
        track has no such effect yet."""
        return None

    # ---- provided

    def uses_graph(self) -> bool:
        return type(self).graph is not Effect.graph

    def expand(self, text: str) -> str:
        words: list[str] = []
        for tok in text.split():
            preset = self.presets.get(tok.lower())
            words += preset[0].split() if preset else [tok]
        return " ".join(words)

    def read(self, text: str) -> dict:
        """Settings from a line that may name presets."""
        return self.parse(self.expand(text))

    def canonical(self, text: str) -> str:
        return self.format(self.read(text))

    def cheat_lines(self) -> tuple[str, ...]:
        if self.cheat:
            return self.cheat
        short = self.aliases[0] if self.aliases else ""
        return (f" {self.name:<5} {short:<2} TRACK {self.syntax}"[:60],)


class FxContext:
    """What an effect may know about the track (or master) it runs on."""

    def __init__(self, project, track: dict):
        self.project = project
        self.track = track
        self.rate: int = project.rate
        self.root: Path = project.root
        bpm = project.get("bpm") or ""
        self.bpm: float | None = float(bpm) if bpm else None
        self.is_master = track.get("owner") == MASTER_OWNER

    def source(self) -> Path:
        return self.project.master if self.is_master else self.project.tracks_dir / self.track["file"]

    def peaks(self) -> bytes | None:
        """Peak per 20 ms of the source audio, 0..128 (master.wav for the master)."""
        path = self.source()
        return (self.project.envelope(self.track.get("file", MASTER_WAV), path) or None) if path.exists() else None

    def spectrum(self) -> bytes | None:
        """Average spectrum of the source audio, 40 bands from 20 Hz to 20 kHz, 0..255."""
        path = self.source()
        return (self.project.spectrum(self.track.get("file", MASTER_WAV), path) or None) if path.exists() else None

    def cache(self, *parts: str) -> Path:
        """A path under the project's .gout/ cache folder, with its parent made."""
        path = self.root.joinpath(".gout", *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


# ---------------------------------------------------------------------------- registry

_REGISTRY: dict[str, Effect] = {}
_LOADED = False
RESERVED = {"master", "all", "presets", "kinds", "add", "move", "clear", "on", "off", "rm"}
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}")


def taken_names() -> set[str]:
    names = set(BASE_COMMANDS) | {a for al in BASE_COMMANDS.values() for a in al} | RESERVED
    for eff in _REGISTRY.values():
        names |= {eff.name, *eff.aliases, *eff.shortcuts}
    return names


def register(effect: Effect, source: str = "built-in") -> None:
    """Add an effect. ValueError when its name or a short name is taken or malformed."""
    if not isinstance(effect, Effect):
        raise ValueError(f"{source}: register() wants an Effect instance, got {type(effect).__name__}")
    words = (effect.name, *effect.aliases, *effect.shortcuts)
    for word in words:
        if not NAME_RE.fullmatch(word):
            raise ValueError(f"{source}: {word!r} is not a usable name (lowercase letters, digits, - or _)")
    taken = taken_names()
    for word in words:
        if word in taken:
            raise ValueError(f"{source}: the name {word!r} is already taken")
    effect.source = source
    _REGISTRY[effect.name] = effect


def effects() -> dict[str, Effect]:
    """Every effect there is: the built-in ones, then the addons."""
    global _LOADED
    if not _LOADED:
        _LOADED = True
        from .effects import builtin
        for eff in builtin():
            register(eff)
        from .addons import load_addons
        load_addons()
    return _REGISTRY


def effect(kind: str) -> Effect | None:
    return effects().get(kind)


def resolve(word: str) -> Effect | None:
    """An effect by its name, a short name, or one of its shortcut commands."""
    word = word.lower()
    for eff in effects().values():
        if word == eff.name or word in eff.aliases or word in eff.shortcuts:
            return eff
    return None
