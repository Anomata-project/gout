"""Addons: Python files in the user's addon folder that register effects and screens.

The folder is $GOUT_ADDONS when set, otherwise $XDG_CONFIG_HOME/gout/addons, otherwise
~/.config/gout/addons. Only that folder is read, never a project folder: an addon is ordinary
Python running with your permissions, and a project someone sends you must not run code.

An addon file defines register(gout) and calls gout.add_effect(SomeEffect()) for each effect:

    from gout.fx import Effect

    class Tremolo(Effect):
        name = "tremolo"
        ...

    def register(gout):
        gout.add_effect(Tremolo())

gout.add_screen(SomeScreen()) adds a full-screen view to the ui (see gout/screens.py).

A file that fails to load is reported and skipped; gout keeps working without it.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from .core import config_home
from .fx import register

API_VERSION = 1  # what AddonApi offers; raised when addons can rely on something new (docs/addons.md)

REPORT: list[dict] = []  # one entry per file tried: file, effects, screens, error


def addon_dir() -> Path:
    env = os.environ.get("GOUT_ADDONS")
    if env:
        return Path(env).expanduser()
    return config_home() / "addons"


class AddonApi:
    """What an addon's register() receives."""

    version = API_VERSION

    def requires(self, version: int) -> None:
        """gout.requires(N) first in register(): a gout older than addon API N says so and
        skips the addon, instead of failing somewhere inside it."""
        if version > API_VERSION:
            raise ValueError(f"it needs gout's addon API {version}, and this gout has {API_VERSION}: update gout")

    def __init__(self, source: str):
        self.source = source
        self.added: list[str] = []
        self.screens: list[str] = []

    def add_effect(self, effect) -> None:
        register(effect, self.source)
        self.added.append(effect.name)

    def add_screen(self, screen) -> None:
        from .screens import register_screen
        register_screen(screen, self.source)
        self.screens.append(screen.name)


def load_addons() -> None:
    REPORT.clear()
    folder = addon_dir()
    if os.environ.get("GOUT_NO_ADDONS") or not folder.is_dir():
        return
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith(("_", ".")):
            continue
        entry = {"file": path, "effects": [], "screens": [], "error": None}
        api = AddonApi(path.name)
        try:
            spec = importlib.util.spec_from_file_location(f"gout_addon_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            hook = getattr(module, "register", None)
            if not callable(hook):
                raise ValueError("it has no register(gout) function")
            hook(api)
        except Exception as exc:  # an addon must never take gout down with it
            entry["error"] = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ValueError) else str(exc)
            print(f"gout: addon {path.name} not loaded: {entry['error']}", file=sys.stderr)
        entry["effects"] = api.added
        entry["screens"] = api.screens
        REPORT.append(entry)
