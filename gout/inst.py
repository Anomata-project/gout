"""Instruments: the class every instrument implements, what it may know, and the registry.

An instrument track has no recording: it has a pattern, steps across and a row per feature, and
the instrument turns that into sound. gout writes the sound as a wav in master/ whenever the
pattern changes, so the mixer, the effects and the timeline see one more track.

    from gout.inst import Instrument
    from gout.pattern import NOTE, DECAY, RELEASE, Feature

    class Beep(Instrument):
        name = "beep"
        summary = "a sine beep"
        features = (NOTE, DECAY, RELEASE)

        def render(self, ctx, pattern, settings):
            ...                      # one array("f") of samples for each channel

    def register(gout):
        gout.requires(2)
        gout.add_instrument(Beep())

Built-in instruments live in gout/instruments/; an addon's has exactly the same shape.
"""
from __future__ import annotations

import re
from array import array
from pathlib import Path

from .pattern import DECAY, DIVISIONS, Feature, MAX_STEPS, NOTE, Pattern, RELEASE, step_edges

# the cells every instrument has in its first row; an instrument's own settings come after them
BASE_SETTINGS = (
    Feature("title", "text", "", summary="what the grid calls the instrument"),
    Feature("description", "text", "", summary="a line about it"),
    Feature("steps", "count", 16, 1, MAX_STEPS, summary="how long the pattern is"),
    Feature("loop", "count", 1, 1, 999, summary="how many times the pattern plays"),
    Feature("step", "choice", "1/16", choices=tuple(DIVISIONS),
            summary="the note value of one step, at the project's bpm"),
)
RESERVED = {"add", "kinds", "all", "clear", "none", "rest", "open"}  # words the instrument command reads itself
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}")
MAX_TAIL_S = 30.0   # what may ring on after the pattern's end


class InstContext:
    """What an instrument may know while it makes its sound."""

    def __init__(self, rate: int, bpm: float, step_s: float, root: Path | None = None):
        self.rate = rate          # samples a second
        self.bpm = bpm
        self.step_s = step_s      # seconds one step lasts
        self.root = root          # the project folder, or None outside a project

    def edges(self, pattern: Pattern) -> list[int]:
        """The sample every step starts at, and where the pattern ends."""
        return step_edges(pattern.steps, self.step_s, self.rate)

    def cache(self, *parts: str) -> Path:
        """A path under the project's .gout/ cache folder, with its parent made."""
        path = self.root.joinpath(".gout", *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class Instrument:
    """One kind of instrument. Subclass it, fill in the attributes, implement render()."""

    name = ""                                   # the kind stored with a track: gout instrument add NAME
    aliases: tuple[str, ...] = ()               # short names
    summary = ""                                # one line for gout instrument kinds and help
    version = 1                                 # raise it when the same pattern sounds different: tracks are written again
    features: tuple[Feature, ...] = (NOTE, DECAY, RELEASE)   # the rows of the grid, top to bottom
    settings: tuple[Feature, ...] = ()          # its own cells in the first row, after title, description, steps, loop, step
    source = "built-in"                         # or the addon file it came from

    def render(self, ctx: InstContext, pattern: Pattern, settings: dict) -> list[array]:
        """One pass of the pattern: an array("f") of samples per channel (one or two), at ctx.rate.
        It may be longer than the pattern by what rings on after the last step; when the pattern
        loops, that tail sounds over the start of the next pass."""
        raise NotImplementedError

    def reads(self, ctx: InstContext, pattern: Pattern, settings: dict) -> list[tuple[str, list[str]]]:
        """Rows the grid shows and nobody types: (name, a text per step). The pitch a fingering
        gives, for one. None by default."""
        return []

    def check(self, ctx: InstContext, pattern: Pattern, settings: dict) -> None:
        """Raise ValueError when the instrument cannot play this as it stands."""

    # ---- provided

    def feature(self, name: str) -> Feature | None:
        return next((f for f in self.features if f.name == name), None)

    def setting(self, name: str) -> Feature | None:
        return next((f for f in (*BASE_SETTINGS, *self.settings) if f.name == name), None)

    def value(self, settings: dict, name: str):
        """A setting as it stands: the cell when there is one, else what the instrument starts with."""
        given = settings.get(name)
        return self.setting(name).default if given is None else given


# ---------------------------------------------------------------------------- registry

_INSTRUMENTS: dict[str, Instrument] = {}


def taken_words() -> set[str]:
    words = set()
    for item in _INSTRUMENTS.values():
        words |= {item.name, *item.aliases}
    return words


def register_instrument(instrument: Instrument, source: str = "built-in") -> None:
    """Add an instrument. ValueError when a name is taken or malformed, or its rows and cells clash."""
    from .fx import taken_names
    if not isinstance(instrument, Instrument):
        raise ValueError(f"{source}: add_instrument() wants an Instrument instance, got {type(instrument).__name__}")
    for word in (instrument.name, *instrument.aliases):
        if not NAME_RE.fullmatch(word):
            raise ValueError(f"{source}: {word!r} is not a usable name (lowercase letters, digits, - or _)")
        if word in taken_names() or word in RESERVED:
            raise ValueError(f"{source}: the name {word!r} is already taken")
    seen: set[str] = set()
    for feature in (*BASE_SETTINGS, *instrument.settings, *instrument.features):
        if not isinstance(feature, Feature) or not NAME_RE.fullmatch(feature.name) or feature.name in RESERVED:
            raise ValueError(f"{source}: {getattr(feature, 'name', feature)!r} is not a usable name for a row or a cell")
        if feature.name in seen:
            raise ValueError(f"{source}: {instrument.name} has two rows or cells called {feature.name!r}")
        seen.add(feature.name)
    if not instrument.features:
        raise ValueError(f"{source}: {instrument.name} has no rows")
    instrument.source = source
    _INSTRUMENTS[instrument.name] = instrument


def register_builtin() -> None:
    """The instruments that come with gout: called once, before the addons load."""
    from .instruments import builtin
    for item in builtin():
        register_instrument(item)


def instruments() -> dict[str, Instrument]:
    """Every instrument there is: the built-in ones, then the addons'."""
    from .fx import effects
    effects()  # loads everything once, addons included
    return _INSTRUMENTS


def instrument(kind: str) -> Instrument | None:
    return instruments().get(kind)


def resolve_instrument(word: str) -> Instrument | None:
    """An instrument by its name or a short name."""
    word = word.lower()
    return next((i for i in instruments().values() if word == i.name or word in i.aliases), None)
