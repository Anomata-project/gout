"""Colours and timeline layout, from color.json.

gout reads the first of these that exists and lays it over the defaults below:
    <project>/color.json          for one project
    ~/.config/gout/color.json     for all of them ($XDG_CONFIG_HOME/gout/color.json)
Unknown keys and bad values are reported and ignored; keys starting with _ are comments.

A colour is "#rrggbb", "#rgb", an xterm colour number (0-255), a name (black, red, green,
yellow, blue, magenta, cyan, white, their bright_ forms, grey, default), or an object:
{"fg": ..., "bg": ..., "bold": true, "dim": true, "underline": true, "reverse": true}.
Colours are mapped to the nearest the terminal has (256 or 8); without colour, every role falls
back to bold, dim or reverse video as before.
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import config_home

FILE_NAME = "color.json"

DEFAULTS: dict = {
    # layout of the timeline
    "master_height": 2,
    "track_height": 2,
    "gap_rows": 1,
    "gap_char": "┈",
    "wave_style": "braille",
    "wave_scale": "linear",
    # the timeline
    "master_wave": {"fg": "#f5c242", "bold": True},
    "master_label": {"fg": "#f5c242", "bold": True},
    "track_palette": ["#5fafd7", "#87d787", "#d787af", "#afafff", "#ffaf5f", "#5fd7af"],
    "track_label": "#d0d0d0",
    "muted_wave": "#585858",
    "trimmed_wave": "#444444",
    "center_line": "#4e4e4e",
    "ruler": "#6c6c6c",
    "ruler_labels": "#9e9e9e",
    "playhead": {"fg": "#ff5f5f", "bold": True},
    "gap_line": "#303030",
    # the rest of the ui
    "header": {"fg": "#000000", "bg": "#87afd7"},
    "prompt": {"fg": "#ffd75f", "bold": True},
    "command_echo": {"fg": "#ffffff", "bold": True},
    "error": "#ff5f5f",
    "suggestion": "#626262",
    "cheat_heading": {"fg": "#87afd7", "bold": True},
    "effect_curve": {"fg": "#f5c242", "bold": True},
    "sheet_edit": {"fg": "#87d787", "bold": True},
}

HELP = {
    "master_height": "rows for the master's waveform, 1 to 6",
    "track_height": "rows for each track's waveform, 1 to 6 (shrinks to 1 when the screen is short)",
    "gap_rows": "empty rows between the master and the tracks and between tracks, 0 to 2",
    "gap_char": "the character the gap rows are drawn with; a space for nothing",
    "wave_style": "braille (fine dots, 2 x 4 per character) or blocks (half blocks, for fonts without braille)",
    "wave_scale": "linear, as DAWs draw waves, or db, which makes quiet passages visible",
    "master_wave": "the master's waveform",
    "master_label": "the master's name and loudness on the left",
    "track_palette": "waveform colours, one per track, repeating",
    "track_label": "track names and flags on the left",
    "muted_wave": "tracks that are muted, or not soloed while something is",
    "trimmed_wave": "material soft-trimmed away, still in the file",
    "center_line": "silence: the centre line where a track is quiet, and the zero line",
    "ruler": "the ruler line under the time labels",
    "ruler_labels": "the times above the ruler",
    "playhead": "the playhead line while playing or stopped part way",
    "gap_line": "the gap rows between tracks",
    "header": "title bars: the prompt's, timeline, effect panel, cheat sheet, sheet",
    "prompt": "the > before what you type",
    "command_echo": "commands you ran, as they appear in the log",
    "error": "error lines in the log and the sheet",
    "suggestion": "the grey rest of a suggestion after the cursor",
    "cheat_heading": "section names in the cheat sheet",
    "effect_curve": "the curve in effect pictures (eq, comp, and so on)",
    "sheet_edit": "new values typed into the parameter sheet",
}

NAMES = {
    "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "bright_black": 8, "grey": 8, "gray": 8, "bright_red": 9, "bright_green": 10, "bright_yellow": 11,
    "bright_blue": 12, "bright_magenta": 13, "bright_cyan": 14, "bright_white": 15,
}
BASIC_RGB = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
             (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
             (255, 0, 255), (0, 255, 255), (255, 255, 255)]
CUBE = (0, 95, 135, 175, 215, 255)

# how each role looks without colour: the ui's look before color.json
MONO = {
    "master_wave": "bold", "master_label": "bold", "track_label": "", "muted_wave": "dim", "trimmed_wave": "dim",
    "center_line": "dim", "ruler": "dim", "ruler_labels": "dim", "playhead": "reverse", "gap_line": "dim",
    "header": "reverse", "prompt": "bold", "command_echo": "bold", "error": "dim", "suggestion": "dim",
    "cheat_heading": "bold", "effect_curve": "bold", "sheet_edit": "bold", "track_palette": "bold",
}


def user_file() -> Path:
    return config_home() / FILE_NAME


def theme_file(project_root: Path | None) -> Path | None:
    for candidate in ([project_root / FILE_NAME] if project_root else []) + [user_file()]:
        if candidate.is_file():
            return candidate
    return None


def rgb_of(text: str) -> tuple[int, int, int] | None:
    t = text.lstrip("#")
    if len(t) == 3 and all(c in "0123456789abcdefABCDEF" for c in t):
        return tuple(int(c * 2, 16) for c in t)
    if len(t) == 6 and all(c in "0123456789abcdefABCDEF" for c in t):
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    return None


def parse_color(value) -> int | tuple[int, int, int] | None:
    """-1 for the terminal's default, 0..255, an (r, g, b), or None when it is not a colour."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 255 else None
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ("default", "none", ""):
        return -1
    if v in NAMES:
        return NAMES[v]
    if v.isdigit() and int(v) <= 255:
        return int(v)
    if v.startswith("#"):
        return rgb_of(v)
    return None


def check_style(value) -> str | None:
    """None when the value is a usable colour or colour object, else what is wrong."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("fg", "bg"):
                if parse_color(item) is None:
                    return f"{key} {item!r} is not a colour"
            elif key in ("bold", "dim", "underline", "reverse"):
                if not isinstance(item, bool):
                    return f"{key} must be true or false"
            else:
                return f"unknown style key {key!r}"
        return None
    return None if parse_color(value) is not None else f"{value!r} is not a colour"


def load_theme(project_root: Path | None = None) -> tuple[dict, list[str], Path | None]:
    """(theme, problems, the file it came from)."""
    theme = json.loads(json.dumps(DEFAULTS))
    problems: list[str] = []
    path = theme_file(project_root)
    if path is None:
        return theme, problems, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return theme, [f"{path}: cannot read it ({exc}); using the defaults"], path
    if not isinstance(data, dict):
        return theme, [f"{path}: expected an object of settings; using the defaults"], path
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key not in DEFAULTS:
            problems.append(f"{key}: not a setting gout knows (gout colors lists them)")
            continue
        wrong = None
        if key in ("master_height", "track_height", "gap_rows"):
            lo, hi = (0, 2) if key == "gap_rows" else (1, 6)
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                wrong = f"a whole number from {lo} to {hi}"
        elif key == "gap_char":
            if not isinstance(value, str) or len(value) > 1:
                wrong = "one character, or empty"
        elif key == "wave_style":
            if value not in ("braille", "blocks"):
                wrong = "braille or blocks"
        elif key == "wave_scale":
            if value not in ("linear", "db"):
                wrong = "linear or db"
        elif key == "track_palette":
            if not isinstance(value, list) or not value or any(check_style(v) for v in value):
                wrong = "a list of colours"
        else:
            reason = check_style(value)
            if reason:
                wrong = reason
        if wrong:
            problems.append(f"{key}: {wrong}; keeping {json.dumps(DEFAULTS[key], ensure_ascii=False)}")
        else:
            theme[key] = value
    return theme, problems, path


def default_document() -> str:
    doc = {"_about": "gout colours and timeline layout. Colours: #rrggbb, #rgb, 0-255, a name, or "
                     "{\"fg\", \"bg\", \"bold\", \"dim\", \"underline\", \"reverse\"}. Delete a key to use the "
                     "default. gout colors shows what is in use.",
           "_help": HELP}
    doc.update(DEFAULTS)
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------- curses

def nearest_256(rgb: tuple[int, int, int]) -> int:
    def cube_index(c):
        return min(range(6), key=lambda i: abs(CUBE[i] - c))
    r, g, b = (cube_index(c) for c in rgb)
    cube_rgb = (CUBE[r], CUBE[g], CUBE[b])
    cube = 16 + 36 * r + 6 * g + b
    level = sum(rgb) / 3
    gray_i = max(0, min(23, round((level - 8) / 10)))
    gray_rgb = (8 + 10 * gray_i,) * 3
    dist = lambda a, c: sum((x - y) ** 2 for x, y in zip(a, c))  # noqa: E731
    return cube if dist(rgb, cube_rgb) <= dist(rgb, gray_rgb) else 232 + gray_i


def nearest_basic(rgb: tuple[int, int, int], count: int) -> int:
    return min(range(min(count, 16)), key=lambda i: sum((x - y) ** 2 for x, y in zip(rgb, BASIC_RGB[i])))


class Palette:
    """Curses attributes for theme roles. Works (in monochrome) before curses is started, so the
    ui can be drawn into a fake screen in tests."""

    def __init__(self, theme: dict):
        self.theme = theme
        self.colors = 0
        self.pairs: dict[tuple[int, int], int] = {}
        self.cache: dict[str, int] = {}

    def enable(self) -> None:
        import curses
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            self.colors = curses.COLORS
        except curses.error:
            self.colors = 0
        self.cache.clear()

    def terminal_color(self, value) -> int:
        c = parse_color(value)
        if c is None or c == -1:
            return -1
        if isinstance(c, tuple):
            return nearest_256(c) if self.colors >= 256 else nearest_basic(c, self.colors)
        return c if c < self.colors else nearest_basic(BASIC_RGB[c] if c < 16 else (128, 128, 128), self.colors)

    def pair(self, fg: int, bg: int) -> int:
        import curses
        key = (fg, bg)
        if key not in self.pairs:
            number = len(self.pairs) + 1
            if number >= getattr(curses, "COLOR_PAIRS", 64):
                return 0
            try:
                curses.init_pair(number, fg, bg)
            except curses.error:
                return 0
            self.pairs[key] = number
        return curses.color_pair(self.pairs[key])

    def style(self, value, mono: str) -> int:
        import curses
        flags = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "underline": curses.A_UNDERLINE,
                 "reverse": curses.A_REVERSE}
        if not self.colors:
            return flags.get(mono, 0)
        spec = value if isinstance(value, dict) else {"fg": value}
        attr = self.pair(self.terminal_color(spec.get("fg", "default")), self.terminal_color(spec.get("bg", "default")))
        for name, flag in flags.items():
            if spec.get(name):
                attr |= flag
        return attr

    def attr(self, role: str) -> int:
        if role not in self.cache:
            self.cache[role] = self.style(self.theme.get(role, DEFAULTS.get(role)), MONO.get(role, ""))
        return self.cache[role]

    def track(self, index: int) -> int:
        palette = self.theme.get("track_palette") or DEFAULTS["track_palette"]
        role = f"track_palette[{index % len(palette)}]"
        if role not in self.cache:
            self.cache[role] = self.style(palette[index % len(palette)], MONO["track_palette"])
        return self.cache[role]
