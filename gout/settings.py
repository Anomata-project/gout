"""Project and master-bus settings: defaults, validation, tags."""
from __future__ import annotations

import re

from .core import die, MASTER_N, MASTER_OWNER, MASTER_WAV, on_off, parse_ms


MASTER_DEFAULTS = {
    "lufs": "off", "ceiling": "-1", "bpm": "", "gain": "0",
    "fadein": "0", "fadeout": "0", "head": "0", "tail": "0",
    "bits": "32f", "mp3": "320k", "title": "", "artist": "", "album": "", "year": "", "comment": "",
}


TAG_KEYS = ("title", "artist", "album", "year", "comment")


BITS_CODEC = {"32f": "pcm_f32le", "24": "pcm_s24le", "16": "pcm_s16le"}


def setting(project: "Project", key: str) -> str:
    return project.get(key) or MASTER_DEFAULTS.get(key, "")


def parse_setting(key: str, value: str) -> tuple[str, str]:
    """Validate a `set` value; returns (canonical key, stored string)."""
    v = value.strip()
    if key in ("autorender", "automix"):
        if v.lower() == "idle":
            return "autorender", "idle"
        return "autorender", "on" if on_off(v, 0) else "off"
    if key == "rate":
        if not v.isdigit() or not 8000 <= int(v) <= 384000:
            die(f"bad sample rate {value!r}")
        return key, v
    if key == "lufs":
        if v.lower() in ("off", "no", "none", "0"):
            return key, "off"
        try:
            target = float(v)
        except ValueError:
            die(f"bad loudness target {value!r}: -14, -16, -23 or off")
        if not -40 <= target <= -5:
            die("the loudness target must be between -40 and -5 LUFS")
        return key, f"{target:g}"
    if key in ("ceiling", "gain"):
        try:
            x = float(v.lower().removesuffix("dbtp").removesuffix("db"))
        except ValueError:
            die(f"bad {key} {value!r}")
        lo, hi = (-20, 0) if key == "ceiling" else (-60, 24)
        if not lo <= x <= hi:
            die(f"{key} must be between {lo} and {hi} dB")
        return key, f"{x:g}"
    if key in ("fadein", "fadeout", "head", "tail"):
        if v.lower() in ("off", "0", "none"):
            return key, "0"
        return key, str(parse_ms(v))
    if key == "bits":
        if v.lower() not in BITS_CODEC:
            die("bits must be 32f, 24 or 16")
        return key, v.lower()
    if key == "mp3":
        if not re.fullmatch(r"\d{2,3}k|v[0-9]", v.lower()):
            die("mp3 quality is a bitrate like 320k or 192k, or v0 .. v9 (VBR, v0 is best)")
        return key, v.lower()
    if key in TAG_KEYS:
        return key, value
    if key == "bpm":
        if v.lower() in ("off", "none", ""):
            return key, ""
        try:
            bpm = float(v)
        except ValueError:
            die(f"bad tempo {value!r}")
        if not 20 <= bpm <= 300:
            die("bpm must be between 20 and 300")
        return key, f"{bpm:g}"
    die(f"unknown setting {key!r} — gout set (with nothing after it) lists them")


def tag_args(project: "Project") -> list[str]:
    out: list[str] = []
    for key in TAG_KEYS:
        value = setting(project, key)
        if value:
            out += ["-metadata", f"{'date' if key == 'year' else key}={value}"]
    return out


def master_track(project: "Project") -> dict:
    """The master bus in the shape of a track row, for the effect code and its pictures."""
    return {"n": MASTER_N, "name": "master", "file": MASTER_WAV, "owner": MASTER_OWNER, "kind": "wav",
            "fx": project.chain(MASTER_OWNER)}


def project_bpm(project: "Project") -> float | None:
    bpm = setting(project, "bpm")
    return float(bpm) if bpm else None
