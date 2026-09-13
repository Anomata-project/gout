"""The project commands behind gout's subcommands and the ui prompt."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
import time
from pathlib import Path

from .core import (
    DB_NAME,
    MASTER_OWNER,
    DEFAULT_RATE,
    die,
    fmt_db,
    fmt_ms,
    fmt_pan,
    fmt_size,
    GoutError,
    is_master,
    MASTER_MP3,
    MASTER_WAV,
    on_off,
    parse_ms,
    run_quiet,
    SIDECAR,
    STEMS_DIR,
    TRACK_DIR,
)
from .media import cut, mp3_frame_cut, probe
from .model import audible, is_heard, timeline
from .fx import Effect, effect, effects, FxContext, resolve
from .settings import BITS_CODEC, MASTER_DEFAULTS, master_track, parse_setting, setting, TAG_KEYS
from .project import legacy_chain, Project
from .mixer import autorender, live_source, mix, sounding_end, track_chain, track_head
from .render import effect_picture, LABEL_W, render_cheat, render_timeline
from .player import Player


class Args:
    """Tiny flag parser. Values like -2s or +1.5s stay positional; unknown -x flags fail."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        self.no_mix = self.flag("--no-mix", "-N")
        self.verbose = self.flag("-v", "--verbose")

    def flag(self, *names: str) -> bool:
        hit = False
        for name in names:
            while name in self.argv:
                self.argv.remove(name)
                hit = True
        return hit

    def value(self, *names: str, default: str | None = None) -> str | None:
        for name in names:
            for i, a in enumerate(self.argv):
                if a == name:
                    if i + 1 >= len(self.argv):
                        die(f"{name} needs a value")
                    val = self.argv[i + 1]
                    del self.argv[i:i + 2]
                    return val
                if a.startswith(name + "="):
                    del self.argv[i]
                    return a[len(name) + 1:]
        return default

    def positionals(self, usage: str, lo: int = 0, hi: int | None = None) -> list[str]:
        for a in self.argv:
            if len(a) > 1 and a[0] == "-" and not (a[1].isdigit() or a[1] in ".+="):
                die(f"unknown flag {a}\nusage: {usage}")
        if len(self.argv) < lo or (hi is not None and len(self.argv) > hi):
            die(f"usage: {usage}")
        return self.argv


def cmd_new(root_hint: Path | None, args: Args) -> None:
    rate_txt = args.value("--rate", "-R", default=str(DEFAULT_RATE))
    (name,) = args.positionals("gout new NAME [-R HZ]", 1, 1)
    if not rate_txt.isdigit() or not 8000 <= int(rate_txt) <= 384000:
        die(f"bad sample rate {rate_txt!r}")
    root = Path.cwd() if name == "." else Path(name)
    if (root / DB_NAME).exists():
        die(f"{root / DB_NAME} already exists")
    project = Project.create(root, int(rate_txt))
    project.sync_json()
    print(f"new   {project.root}  ({project.rate} Hz, tracks go in {TRACK_DIR}/)")


def ingest(project: Project, src: Path, name: str | None, at_ms: int, verbose: bool) -> tuple[dict, bool]:
    """Register src as a track; copies it into master/ unless it already lives there.

    Returns (track, created) where created says whether a new file was written.
    """
    if not src.is_file():
        die(f"no such file: {src}" + ("  (a name with spaces needs quotes, or a \\ before each space;"
                                      " in the ui, tab completes it)" if " " not in str(src) else ""))
    info = probe(src)
    suffix = src.suffix.lower()
    if info["codec"] == "mp3" and suffix == ".mp3":
        kind, ext = "mp3", ".mp3"
    elif info["codec"].startswith("pcm_") and suffix == ".wav":
        kind, ext = "wav", ".wav"
    else:
        kind, ext = "wav", ".wav"  # anything else is decoded to wav on the way in

    in_place = src.resolve().parent == project.tracks_dir.resolve() and ext == suffix
    if in_place and not any(t["file"] == src.name for t in project.tracks()):
        taken = {t["name"] for t in project.tracks()}
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", name or src.stem).strip("-.") or "track"
        track_name, i = base, 2
        while track_name in taken:
            track_name, i = f"{base}-{i}", i + 1
        dst = src
    else:
        track_name = project.unique_name(name or src.stem)
        dst = project.tracks_dir / f"{track_name}{ext}"
        if kind == "mp3" or (kind == "wav" and suffix == ".wav" and info["codec"].startswith("pcm_")):
            shutil.copy2(src, dst)
        else:
            run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                       "-map", "0:a:0", "-c:a", "pcm_s24le", str(dst)], verbose)
            info = probe(dst)

    track = project.insert(
        name=track_name, file=dst.name, kind=kind,
        length_ms=round(info["duration"] * 1000), channels=info["channels"] or 2,
        sample_rate=info["sample_rate"] or project.rate, offset_ms=at_ms,
    )
    project.envelope(dst.name, dst)  # so the timeline can draw it without a pause later
    how = "kept in" if dst == src else ("copied to" if ext == suffix else "converted to")
    print(f"add   {track['n']:>2}  {track['name']:<16} {kind}  {info['channels']}ch  {info['sample_rate']} Hz"
          f"  {fmt_ms(track['length_ms'])}  at {fmt_ms(at_ms)}  ({how} {TRACK_DIR}/{dst.name})")
    return track, dst != src


def rejoin_paths(words: list[str]) -> list[str]:
    """Put back file names that were split at their spaces: `add Sandi piano .m4a` finds
    "Sandi piano .m4a" when no file is called "Sandi". Words that already name a file stay."""
    out, i = [], 0
    while i < len(words):
        if Path(words[i]).expanduser().exists():
            out.append(words[i])
            i += 1
            continue
        for j in range(len(words), i + 1, -1):
            joined = " ".join(words[i:j])
            if Path(joined).expanduser().exists():
                out.append(joined)
                i = j
                break
        else:
            out.append(words[i])
            i += 1
    return out


def cmd_add(project: Project, args: Args) -> None:
    name = args.value("--name", "-n")
    at = args.value("--at", "-a")
    files = rejoin_paths(args.positionals("gout add FILE... [-n NAME] [-a TIME] [-N]", 1))
    if name and len(files) > 1:
        die("--name works with a single file")
    at_ms = parse_ms(at) if at else 0
    project.record("add " + " ".join(files))
    made: list[str] = []
    for f in files:
        track, created = ingest(project, Path(f), name, at_ms, args.verbose)
        if created:
            made.append(track["file"])
    project.created(made)
    autorender(project, args)


def cmd_scan(project: Project, args: Args) -> None:
    """Register files that appeared in master/ by hand; report tracks whose file is gone."""
    args.positionals("gout scan [-N]")
    tracks = project.tracks()
    known = {t["file"] for t in tracks}
    files = sorted(f for f in project.tracks_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in (".wav", ".mp3")
                   and not f.name.startswith(".") and ".part" not in f.name)
    new = [f for f in files if f.name not in known]
    missing = [t for t in tracks if not (project.tracks_dir / t["file"]).exists()]
    if not new and not missing:
        print(f"scan  {TRACK_DIR}/ matches {DB_NAME}: {len(files)} file{'' if len(files) == 1 else 's'},"
              f" nothing new, nothing missing")
        return
    if new:
        project.record("scan " + " ".join(f.name for f in new))
        for f in new:
            ingest(project, f, None, 0, args.verbose)
    for t in missing:
        print(f"scan  missing  {t['n']:>2}  {t['name']:<16} {TRACK_DIR}/{t['file']} is gone"
              f"  (gout rm {t['n']} drops the track, or put the file back)")
    if new:
        autorender(project, args)


def cmd_ls(project: Project, args: Args) -> None:
    args.positionals("gout ls")
    tracks = project.tracks()
    print(f"proj  {project.get('name')}  {project.rate} Hz  {len(tracks)} track"
          f"{'' if len(tracks) == 1 else 's'}  {TRACK_DIR}/  autorender {project.render_mode}")
    if not tracks:
        print("      no tracks yet — gout add FILE")
        return
    any_solo = any(t["solo"] for t in tracks)
    print(f"{'n':>4}  {'name':<16} {'kind':<4} {'ch':>2}  {'at':<12} {'length':<12} {'file':<12} "
          f"{'trim':<27} {'gain':<7} {'pan':<4} flags")
    for t in tracks:
        a, b = audible(t)
        start, _ = timeline(t)
        trimmed = a > 0 or b < t["length_ms"]
        trim = f"{fmt_ms(a)} > {fmt_ms(b)}" if trimmed else "-"
        flags = ("M" if t["mute"] else "-") + ("S" if t["solo"] else "-")
        if not is_heard(t, any_solo):
            flags += " (silent)"
        if t["fx"]:
            flags += "  fx " + " | ".join(fx_text(item) for item in t["fx"])
        print(f"{t['n']:>4}  {t['name']:<16} {t['kind']:<4} {t['channels']:>2}  {fmt_ms(start):<12} "
              f"{fmt_ms(b - a):<12} {fmt_ms(t['length_ms']):<12} {trim:<27} "
              f"{fmt_db(t['gain_db']):<7} {fmt_pan(t['pan']):<4} {flags}")
    master_ms = project.get("master_ms")
    if master_ms and project.master.exists():
        lufs, tp = project.get("master_lufs"), project.get("master_tp")
        loud = f"  {float(lufs):.1f} LUFS  peak {float(tp):+.1f} dBTP" if lufs and tp else ""
        stale = "" if project.master_is_current() else "  (out of date: play streams the project live, mix renders it)"
        print(f"      {MASTER_WAV}  {fmt_ms(int(master_ms))}{loud}{stale}")
    else:
        print(f"      {MASTER_WAV} not rendered — gout mix")


def cmd_move(project: Project, args: Args) -> None:
    spec, delta = args.positionals("gout move TRACK +TIME | -TIME | TIME  (or =TIME)", 2, 2)
    t = project.track(spec)
    a, _ = audible(t)
    if delta[0] in "+-":
        step = parse_ms(delta[1:])
        offset = t["offset_ms"] + (step if delta[0] == "+" else -step)
    else:
        text = delta[1:] if delta[0] == "=" else delta
        neg = text.startswith("-")
        at = parse_ms(text.lstrip("+-"))
        offset = (-at if neg else at) - a  # place the audible start there
    project.record(f"move {t['name']} {delta}")
    project.update(t["n"], offset_ms=offset)
    start, end = timeline({**t, "offset_ms": offset})
    print(f"move  {t['n']:>2}  {t['name']:<16} at {fmt_ms(start)} -> {fmt_ms(end)}")
    autorender(project, args)


TRIM_USAGE = ("gout trim TRACK [-st TIME] [-et TIME | -el TIME] [-c]          soft: file untouched\n"
              "       gout trim TRACK -H [-st ..] [-et ..|-el ..] [-r]           hard: rewrite the file\n"
              "       times are measured from the start of the track's own file")


def cmd_trim(project: Project, args: Args) -> None:
    hard = args.flag("--hard", "-H")
    clear = args.flag("--clear", "-c")
    reencode = args.flag("-r", "--reencode")
    st, et, el = args.value("-st", "--start"), args.value("-et", "--end"), args.value("-el", "--length")
    (spec,) = args.positionals(TRIM_USAGE, 1, 1)
    if et and el:
        die("-et and -el are exclusive\n" + TRIM_USAGE)
    t = project.track(spec)
    length = t["length_ms"]
    a, b = audible(t)
    if clear:
        a, b = 0, length
    if st:
        a = parse_ms(st)
    if et:
        b = parse_ms(et)
    elif el:
        b = a + parse_ms(el)
    b = min(b, length)
    if a < 0 or a >= length:
        die(f"in point {fmt_ms(a)} is outside the file ({fmt_ms(length)} long)")
    if b <= a:
        die(f"out point {fmt_ms(b)} is not after the in point {fmt_ms(a)}")

    if not (hard or clear or st or et or el):
        trimmed = a > 0 or b < length
        print(f"trim  {t['n']:>2}  {t['name']:<16} " + (f"{fmt_ms(a)} > {fmt_ms(b)}  of {fmt_ms(length)}"
              if trimmed else f"none (whole file, {fmt_ms(length)})") + "  soft")
        return

    if not hard:
        project.record(f"trim {t['name']} {fmt_ms(a)}>{fmt_ms(b)}")
        project.update(t["n"], in_ms=a, out_ms=None if b >= length else b)
        start, end = timeline({**t, "in_ms": a, "out_ms": b})
        print(f"trim  {t['n']:>2}  {t['name']:<16} {fmt_ms(a)} > {fmt_ms(b)}  soft, {fmt_ms(b - a)} audible"
              f"  at {fmt_ms(start)} -> {fmt_ms(end)}")
        autorender(project, args)
        return

    if a == 0 and b >= length:
        die("nothing to cut: the whole file is already the audible part (soft-trim first, or give -st/-et)")
    src = project.tracks_dir / t["file"]
    tmp = src.with_name(f"{src.stem}.part{src.suffix}")
    info = probe(src)
    project.record(f"trim {t['name']} --hard {fmt_ms(a)}>{fmt_ms(b)}", undoable=False)
    try:
        if t["kind"] == "mp3" and not reencode:
            head, snap_a, snap_b = mp3_frame_cut(src, tmp, a, b, args.verbose)
            how = f"mp3 frames {fmt_ms(snap_a)} > {fmt_ms(snap_b)}, not re-encoded"
        else:
            codec = info["codec"] if info["codec"].startswith("pcm_") else "mp3"
            cut(src, tmp, a / 1000, (b - a) / 1000, reencode, info["bitrate"], args.verbose, codec)
            head = a
            how = "re-encoded, exact" if t["kind"] == "mp3" else "wav, exact"
        tmp.replace(src)
    finally:
        tmp.unlink(missing_ok=True)
    new_len = round(probe(src)["duration"] * 1000)
    project.envelope(src.name, src)
    offset = t["offset_ms"] + head
    project.update(t["n"], offset_ms=offset, in_ms=0, out_ms=None, length_ms=new_len)
    start, end = timeline({**t, "offset_ms": offset, "in_ms": 0, "out_ms": None, "length_ms": new_len})
    print(f"trim  {t['n']:>2}  {t['name']:<16} {fmt_ms(a)} > {fmt_ms(b)}  hard ({how})")
    print(f"      {TRACK_DIR}/{t['file']}  {fmt_ms(length)} -> {fmt_ms(new_len)}"
          f"  at {fmt_ms(start)} -> {fmt_ms(end)}  (position kept; not undoable)")
    autorender(project, args)


def cmd_rm(project: Project, args: Args) -> None:
    delete = args.flag("-D", "--delete")
    (spec,) = args.positionals("gout rm TRACK [-D]", 1, 1)
    t = project.track(spec)
    project.record(f"rm {t['name']}" + (" -D" if delete else ""), undoable=not delete)
    project.delete(t["n"])
    path = project.tracks_dir / t["file"]
    if delete and path.exists():
        path.unlink()
        project.forget_envelope(t["file"])
        print(f"rm    {t['name']}  (deleted {TRACK_DIR}/{t['file']})")
    else:
        print(f"rm    {t['name']}  ({TRACK_DIR}/{t['file']} kept; gout add {TRACK_DIR}/{t['file']} brings it back)")
    autorender(project, args)


def _toggle(project: Project, args: Args, column: str) -> None:
    pos = args.positionals(f"gout {column} TRACK|all [on|off]", 1, 2)
    spec, state = pos[0], (pos[1] if len(pos) > 1 else None)
    if spec == "all":
        if state is None:
            die(f"gout {column} all needs on or off")
        targets = project.tracks()
    else:
        targets = [project.track(spec)]
    project.record(f"{column} {spec} {state or ''}".strip())
    for t in targets:
        new = on_off(state, t[column])
        project.update(t["n"], **{column: new})
        print(f"{column:<5} {t['n']:>2}  {t['name']:<16} {'on' if new else 'off'}")
    autorender(project, args)


def cmd_mute(project: Project, args: Args) -> None:
    _toggle(project, args, "mute")


def cmd_solo(project: Project, args: Args) -> None:
    _toggle(project, args, "solo")


def cmd_gain(project: Project, args: Args) -> None:
    spec, value = args.positionals("gout gain TRACK DB   (e.g. gain 2 -6)", 2, 2)
    t = project.track(spec)
    try:
        gain = float(value.lower().removesuffix("db"))
    except ValueError:
        die(f"bad gain {value!r}, expected a number of dB like -6 or +3.5")
    if not -60 <= gain <= 24:
        die("gain must be between -60 and +24 dB")
    project.record(f"gain {t['name']} {value}")
    project.update(t["n"], gain_db=gain)
    print(f"gain  {t['n']:>2}  {t['name']:<16} {fmt_db(gain)}")
    autorender(project, args)


def cmd_pan(project: Project, args: Args) -> None:
    spec, value = args.positionals("gout pan TRACK C | L30 | R30 | -100..100", 2, 2)
    t = project.track(spec)
    v = value.strip().upper()
    match = re.fullmatch(r"([LR])\s*(\d{1,3})", v)
    if v in ("C", "CENTER", "CENTRE", "0"):
        pan = 0.0
    elif match:
        pan = int(match.group(2)) / 100 * (-1 if match.group(1) == "L" else 1)
    else:
        try:
            pan = float(v) / 100
        except ValueError:
            die(f"bad pan {value!r}")
    if not -1 <= pan <= 1:
        die("pan must be between L100 and R100")
    project.record(f"pan {t['name']} {value}")
    project.update(t["n"], pan=pan)
    print(f"pan   {t['n']:>2}  {t['name']:<16} {fmt_pan(pan)}")
    autorender(project, args)


SET_USAGE = "gout set KEY VALUE   (gout set alone lists the keys and their values)"


def fx_text(item: dict) -> str:
    eff = effect(item["kind"])
    text = f"{item['kind']} {item['params'] or (eff.empty if eff else '')}".rstrip()
    if not item["on"]:
        text += " (off)"
    if eff is None:
        text += " (not installed)"
    return text


def chain_owner(project: Project, spec: str) -> dict:
    """A track, or the master bus in the shape of one."""
    return master_track(project) if is_master(spec) else project.track(spec)


def owner_label(t: dict) -> str:
    return "master" if t["owner"] == MASTER_OWNER else t["name"]


def owner_spec(t: dict) -> str:
    return "master" if t["owner"] == MASTER_OWNER else str(t["n"])


def refresh(project: Project, t: dict) -> dict:
    return chain_owner(project, owner_spec(t))


def effect_usage(eff: Effect) -> str:
    text = (f"gout {eff.name} TRACK|master [SETTINGS... | PRESET | on | off | clear]\n"
            f"       e.g. gout {eff.name} 3 {eff.syntax}")
    if eff.presets:
        text += f"\n       presets: {' '.join(eff.presets)}   (gout {eff.name} presets explains them)"
    return text


def checked_line(project: Project, t: dict, eff: Effect, text: str) -> str:
    """The canonical settings line, after the effect has read and checked it."""
    try:
        params = eff.read(text)
    except ValueError as exc:
        die(f"{exc}\n{effect_usage(eff)}")
    eff.check(FxContext(project, t), params)
    return eff.format(params)


def default_position(t: dict, eff: Effect) -> int:
    """Where an effect goes in a chain by its order: before the first one that comes later."""
    for pos, item in enumerate(t["fx"], 1):
        other = effect(item["kind"])
        if (other.order if other else 50) > eff.order:
            return pos
    return len(t["fx"]) + 1


def first_of(t: dict, kind: str) -> dict | None:
    return next((item for item in t["fx"] if item["kind"] == kind), None)


def effect_line(eff: Effect, t: dict, item: dict | None) -> str:
    n = 0 if t["owner"] == MASTER_OWNER else t["n"]
    text = "none" if item is None else (item["params"] or eff.empty)
    if item is not None and not item["on"]:
        text += f"  (off: bypassed, gout {eff.name} {owner_spec(t)} on brings it back)"
    return f"{eff.name:<5} {n:>2}  {owner_label(t):<16} {text}"


def show_effect(project: Project, t: dict, eff: Effect, item: dict | None) -> None:
    width = min(100, shutil.get_terminal_size((100, 24)).columns) - 6
    rows = effect_picture(project, t, eff, item, min(width, eff.picture_width[1]), eff.picture_height)
    for text, _, _ in rows:
        if text.strip():
            print("      " + text)
    if eff.legend:
        print("      " + eff.legend)


def print_presets(eff: Effect) -> None:
    print(f"presets  a name stands for these settings; add your own after it: gout {eff.name} 3 "
          f"{next(iter(eff.presets), '')} ...")
    width = max((len(line) for line, _ in eff.presets.values()), default=4)
    for name, (line, what) in eff.presets.items():
        print(f"  {name:<10} {line or eff.empty:<{width}}  {what}")


def make_effect_command(eff: Effect):
    """gout NAME TRACK [SETTINGS | PRESET | on | off | clear]: the first effect of this kind."""

    def command(project: Project, args: Args) -> None:
        pos = args.positionals(effect_usage(eff), 1)
        if pos[0].lower() in ("presets", "preset"):
            print_presets(eff)
            return
        t = chain_owner(project, pos[0])
        item = first_of(t, eff.name)
        words = pos[1:]
        if not words:
            print(effect_line(eff, t, item))
            show_effect(project, t, eff, item)
            return
        low = [w.lower() for w in words]
        who = owner_label(t)
        if low in (["on"], ["off"]):
            if item is None:
                die(f"{who} has no {eff.name} to switch {low[0]}: gout {eff.name} {pos[0]} SETTINGS adds one")
            project.record(f"{eff.name} {who} {low[0]}")
            project.fx_set(item["id"], on=low[0] == "on")
        elif low in (["clear"], ["rm"], ["none"], ["remove"]):
            if item is not None:
                project.record(f"{eff.name} {who} clear")
                project.fx_remove(item["id"])
        else:
            line = checked_line(project, t, eff, " ".join(words))
            project.record(f"{eff.name} {who} {' '.join(words)}")
            if item is None:
                project.fx_insert(t["owner"], eff.name, line, default_position(t, eff))
            else:
                project.fx_set(item["id"], params=line, on=True)
        t = refresh(project, t)
        print(effect_line(eff, t, first_of(t, eff.name)))
        autorender(project, args)

    command.__doc__ = eff.summary
    return command


def make_shortcut_command(eff: Effect, name: str, usage: str, apply):
    """A command like `hp TRACK 80` that edits the first effect of a kind through a function."""

    def command(project: Project, args: Args) -> None:
        pos = args.positionals(f"gout {name} TRACK|master {usage}", 2)
        t = chain_owner(project, pos[0])
        item = first_of(t, eff.name)
        try:
            line = apply(item["params"] if item else "", pos[1:])
        except ValueError as exc:
            die(str(exc))
        line = checked_line(project, t, eff, line)
        project.record(f"{name} {owner_label(t)} {' '.join(pos[1:])}")
        if item is None:
            project.fx_insert(t["owner"], eff.name, line, default_position(t, eff))
        else:
            project.fx_set(item["id"], params=line, on=True)
        t = refresh(project, t)
        print(effect_line(eff, t, first_of(t, eff.name)))
        autorender(project, args)

    return command


def effect_commands() -> dict:
    """A command per effect and per shortcut, from the registry."""
    table = {}
    for eff in effects().values():
        table[eff.name] = make_effect_command(eff)
        for name, (usage, _summary, apply) in eff.shortcuts.items():
            table[name] = make_shortcut_command(eff, name, usage, apply)
    return table


FX_USAGE = """gout fx TRACK|master                          the effects in order, numbered
       gout fx TRACK add KIND [SETTINGS...]          add one at the end
       gout fx TRACK N SETTINGS... | on | off | rm   change, bypass or remove slot N
       gout fx TRACK N move M                        move slot N to position M
       gout fx TRACK clear                           remove them all
       gout fx kinds                                 every effect there is"""


def print_chain(t: dict) -> None:
    items = t["fx"]
    print(f"fx    {owner_spec(t):>2}  {owner_label(t):<16} {len(items)} effect{'' if len(items) == 1 else 's'}"
          + ("" if items else f"  (gout fx {owner_spec(t)} add KIND, or gout eq {owner_spec(t)} hp80)"))
    for pos, item in enumerate(items, 1):
        print(f"      {pos:>2}  {fx_text(item)}")


def slot_of(t: dict, spec: str) -> dict:
    """Slot N (1-based) or #ID (as the parameter sheet uses) in a chain."""
    items = t["fx"]
    if spec.startswith("#") and spec[1:].isdigit():
        item = next((i for i in items if i["id"] == int(spec[1:])), None)
        if item is None:
            die(f"{owner_label(t)} has no effect {spec} (the chain changed?)")
        return item
    if spec.isdigit() and 1 <= int(spec) <= len(items):
        return items[int(spec) - 1]
    die(f"{owner_label(t)} has {len(items)} effect{'' if len(items) == 1 else 's'}; no slot {spec!r}\n{FX_USAGE}")


def print_kinds() -> None:
    """Every effect there is, built in or from an addon. Needs no project."""
    rows = [("/".join((eff.name, *eff.aliases)), eff) for eff in effects().values()]
    width = max(len(names) for names, _ in rows)
    for names, eff in rows:
        origin = "" if eff.source == "built-in" else f"  [addon {eff.source}]"
        print(f"  {names:<{width}}  {eff.summary}{origin}")


def cmd_fx(project: Project, args: Args) -> None:
    pos = args.positionals(FX_USAGE, 1)
    if pos[0].lower() in ("kinds", "effects"):
        print_kinds()
        return
    t = chain_owner(project, pos[0])
    words = pos[1:]
    who = owner_label(t)
    if not words:
        print_chain(t)
        return
    low = [w.lower() for w in words]
    if low[0] == "add":
        if len(words) < 2:
            die(FX_USAGE)
        eff = resolve(words[1])
        if eff is None:
            die(f"no effect called {words[1]!r}; gout fx kinds lists them")
        line = checked_line(project, t, eff, " ".join(words[2:]))
        project.record(f"fx {who} add {' '.join(words[1:])}")
        project.fx_insert(t["owner"], eff.name, line)
    elif low == ["clear"]:
        project.record(f"fx {who} clear")
        project.fx_clear(t["owner"])
    else:
        item = slot_of(t, words[0])
        rest, low_rest = words[1:], low[1:]
        if not rest:
            die(FX_USAGE)
        if low_rest[0] == "move":
            if len(rest) != 2 or not rest[1].isdigit():
                die(f"move needs a position: gout fx {owner_spec(t)} {words[0]} move 1")
            project.record(f"fx {who} {words[0]} move {rest[1]}")
            project.fx_move(item["id"], int(rest[1]))
        elif low_rest in (["on"], ["off"]):
            project.record(f"fx {who} {words[0]} {low_rest[0]}")
            project.fx_set(item["id"], on=low_rest[0] == "on")
        elif low_rest in (["rm"], ["remove"], ["clear"], ["none"]):
            project.record(f"fx {who} {words[0]} rm")
            project.fx_remove(item["id"])
        else:
            eff = effect(item["kind"])
            if eff is None:
                die(f"{item['kind']} is not installed, so its settings cannot be checked")
            line = checked_line(project, t, eff, " ".join(rest))
            project.record(f"fx {who} {words[0]} {' '.join(rest)}")
            project.fx_set(item["id"], params=line, on=True)
    print_chain(refresh(project, t))
    autorender(project, args)


def apply_chain(project: Project, owner: str, who: str, items: list, warnings: list[str]) -> None:
    """Replace a chain with items from a document; unknown kinds are kept as they are."""
    project.fx_clear(owner)
    for item in items:
        if not isinstance(item, dict) or not item.get("kind"):
            continue
        kind, params, on = str(item["kind"]), str(item.get("params") or ""), bool(item.get("on", True))
        eff = effect(kind)
        if eff is None:
            warnings.append(f"{who}: {kind} is not installed; kept, but left out of the mix until it is")
        else:
            try:
                params = eff.canonical(params)
            except ValueError as exc:
                warnings.append(f"{who}: {kind} ignored ({exc})")
                continue
        project.fx_insert(owner, kind, params, on=on)


def cmd_set(project: Project, args: Args) -> None:
    pos = args.positionals(SET_USAGE, 0)
    if not pos:
        hints = {
            "autorender": "idle: render in the ui's quiet moments; on: after every change; off: only mix",
            "lufs": "loudness target: -14 (streaming) -16 (Apple) -23 (broadcast) or off",
            "ceiling": "true-peak ceiling in dBTP for the loudness step",
            "bpm": "tempo, so delays can be note values like 1/8",
            "gain": "master gain in dB, before the loudness step",
            "fadein": "e.g. 500ms", "fadeout": "e.g. 3s", "head": "silence before, e.g. 500ms",
            "tail": "silence after, e.g. 2s", "bits": "32f | 24 | 16 (dithered)",
            "mp3": "bounce quality: 320k, 192k, v0 .. v9",
        }
        print(f"{'name':<11} {project.get('name')}")
        print(f"{'rate':<11} {project.rate}")
        print(f"{'autorender':<11} {project.render_mode:<14} {hints['autorender']}")
        for key in MASTER_DEFAULTS:
            value = setting(project, key)
            if key in ("fadein", "fadeout", "head", "tail") and value != "0":
                value = fmt_ms(int(value))
            elif key in TAG_KEYS:
                value = value or "-"
            elif key == "bpm":
                value = value or "-"
            print(f"{key:<11} {value:<14} {hints.get(key, '')}")
        chain = master_track(project)["fx"]
        print(f"{'effects':<11} {' | '.join(fx_text(i) for i in chain) or 'none':<14} "
              f"{'' if chain else 'the master chain: gout fx master add KIND'}")
        return
    eff = resolve(pos[0]) if pos[0].lower() in effects() else None
    if eff is not None:  # set eq hp30: the master's eq, as gout eq master hp30
        effect_commands()[eff.name](project, Args(["master", *pos[1:]] + (["-N"] if args.no_mix else [])))
        return
    if len(pos) < 2 or (len(pos) > 2 and pos[0].lower() not in TAG_KEYS):
        die(SET_USAGE)
    key, value = parse_setting(pos[0].lower(), " ".join(pos[1:]))  # tags may span words
    if key == "bpm" and not value:
        users = []
        for t in project.tracks() + [master_track(project)]:
            ctx = FxContext(project, t)
            ctx.bpm = None
            for item in t["fx"]:
                eff = effect(item["kind"])
                try:
                    if eff is not None:
                        eff.check(ctx, eff.read(item["params"]))
                except (GoutError, ValueError):
                    users.append(f"the {item['kind']} on {owner_label(t)}")
        if users:
            die(f"cannot clear bpm: {', '.join(users)} uses the tempo; give it a time in ms first")
    project.record(f"set {key} {value}")
    project.set(key, value)
    shown = fmt_ms(int(value)) if key in ("fadein", "fadeout", "head", "tail") and value != "0" else value
    print(f"set   {key} {shown or '(cleared)'}")
    if key != "autorender":
        autorender(project, args)


def cmd_stats(project: Project, args: Args) -> None:
    args.positionals("gout stats")
    tracks = project.tracks()
    if not tracks:
        die("the project has no tracks yet — gout add FILE")
    print(f"stats {'n':>2}  {'name':<16} {'file LUFS':>9} {'LRA':>5} {'peak dBTP':>9} {'gain':>7} {'-> LUFS':>8}  flags")
    for t in tracks:
        m = project.loudness(t["file"], project.tracks_dir / t["file"])
        flags = ("M" if t["mute"] else "-") + ("S" if t["solo"] else "-")
        if m is None or m["i"] < -70:
            print(f"{t['n']:>8}  {t['name']:<16} {'silent':>9} {'':>5} {'':>9} {fmt_db(t['gain_db']):>7} {'':>8}  {flags}")
            continue
        print(f"{t['n']:>8}  {t['name']:<16} {m['i']:>9.1f} {m['lra']:>5.1f} {m['tp']:>+9.1f} "
              f"{fmt_db(t['gain_db']):>7} {m['i'] + t['gain_db']:>8.1f}  {flags}")
    lufs, tp, lra = (project.get(k) for k in ("master_lufs", "master_tp", "master_lra"))
    target, ceiling = setting(project, "lufs"), setting(project, "ceiling")
    goal = f"target {target} LUFS under {ceiling} dBTP" if target != "off" else "no loudness target (set lufs -14)"
    if lufs and tp and lra and project.master.exists():
        print(f"{'':>8}  {MASTER_WAV:<16} {float(lufs):>9.1f} {float(lra):>5.1f} {float(tp):>+9.1f} {'':>7} {'':>8}  {goal}")
    else:
        print(f"{'':>8}  {MASTER_WAV:<16} not rendered — gout mix   ({goal})")


def cmd_mix(project: Project, args: Args) -> None:
    mp3 = args.flag("--mp3", "-3")
    args.positionals("gout mix [-3] [-v]")
    mix(project, args.verbose, mp3)


def player_for(project: Project, from_ms: int, render: bool = False) -> Player:
    """A player for the project from from_ms: master.wav when it matches the project (or after
    rendering it, with render), otherwise the project streamed live. Not started yet."""
    if render and not project.master_is_current():
        print(f"play  rendering {MASTER_WAV} first")
        mix(project)
    if project.master_is_current():
        length = probe(project.master)["duration"]
        head = head_seconds(project)
        if from_ms / 1000 + head >= length:
            die(f"{fmt_ms(from_ms)} is past the end of {MASTER_WAV} ({fmt_ms((length - head) * 1000)})")
        return Player(project.master, from_ms / 1000 + head, length)
    warnings: set[str] = set()
    source = live_source(project, from_ms, warnings)
    for warning in sorted(warnings):
        print(f"      {warning}")
    if source is None:
        die("nothing to play: no track is audible there" + (f" from {fmt_ms(from_ms)}" if from_ms else ""))
    args, length_ms = source
    if length_ms <= 0:
        die(f"{fmt_ms(from_ms)} is past the end of the project")
    return Player(None, from_ms / 1000, (from_ms + length_ms) / 1000, stream=args)


def head_seconds(project: Project) -> float:
    """master.wav starts with the head padding, so project time 0 is this far into the file."""
    return int(setting(project, "head")) / 1000


def progress_bar(position: float, length: float, width: int = 30) -> str:
    done = round(width * position / length) if length else 0
    return "━" * done + "─" * (width - done)


def cmd_play(project: Project, args: Args) -> None:
    render = args.flag("--render", "-r")
    pos = args.positionals("gout play [FROM] [-r]   e.g. gout play 1:30  (-r renders first; ctrl-c stops)", 0, 1)
    start = parse_ms(pos[0]) / 1000 if pos else 0.0
    player = player_for(project, round(start * 1000), render).start()
    head = 0.0 if player.live else head_seconds(project)
    length = player.length_s
    what = "live (master.wav is out of date; mix or play -r renders it)" if player.live else MASTER_WAV
    print(f"play  {what} from {fmt_ms(start * 1000)}  ({player.backend}; ctrl-c stops)")
    live = sys.stdout.isatty()
    try:
        while player.running():
            if live:
                now = player.position()
                print(f"\r      ▶ {fmt_ms((now - head) * 1000)} / {fmt_ms((length - head) * 1000)}  "
                      f"{progress_bar(now, length)}", end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        where = player.position() - head
        player.stop()
        print(("\n" if live else "") + f"play  stopped at {fmt_ms(where * 1000)}"
              f"  (gout play {fmt_ms(max(0, where) * 1000)} carries on from there)")
        return
    print(("\n" if live else "") + "play  finished")


def cmd_stems(project: Project, args: Args) -> None:
    """One file per track, processed as in the mix, all the same length from 0:00."""
    audible_only = args.flag("--audible", "-A")
    pos = args.positionals(f"gout stems [DIR] [-A]   (default {STEMS_DIR}/ in the project; -A: only what the mix hears)", 0, 1)
    tracks = project.tracks()
    if not tracks:
        die("the project has no tracks yet — gout add FILE")
    any_solo = any(t["solo"] for t in tracks)
    plans = []
    for t in tracks:
        if audible_only and not is_heard(t, any_solo):
            continue
        if track_head(project, t) is not None:
            plans.append((t, None))
    if not plans:
        die("nothing to export: no track has audible material" + (" the mix hears" if audible_only else ""))
    out_dir = Path(pos[0]).expanduser() if pos else project.root / STEMS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    end_ms = max(1, max(sounding_end(project, t) for t, _ in plans))
    bits = setting(project, "bits")
    warnings: set[str] = set()
    print(f"stems {out_dir}  {len(plans)} track{'' if len(plans) == 1 else 's'}, {fmt_ms(end_ms)} each"
          f"  ({bits}, {project.rate} Hz, stereo)")
    for t, steps in plans:
        name = f"{t['n']:02d}-{t['name']}.wav"
        inputs = [project.tracks_dir / t["file"]]
        graph = track_chain(project, t, "[0:a]", "s", inputs, warnings) + f";[s]apad=whole_dur={end_ms / 1000:.3f}[out]"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for path in inputs:
            cmd += ["-i", str(path)]
        cmd += ["-filter_complex", graph, "-map", "[out]", "-ar", str(project.rate)]
        if bits == "16":
            cmd += ["-dither_method", "triangular"]
        cmd += ["-c:a", BITS_CODEC[bits], str(out_dir / name)]
        run_quiet(cmd, args.verbose)
        state = "" if is_heard(t, any_solo) else "  (muted or not soloed in the mix)"
        print(f"      {name:<26} {fmt_size((out_dir / name).stat().st_size):>10}{state}")
    for warning in sorted(warnings):
        print(f"      {warning}")
    master_bits = [k for k in ("gain", "fadein", "fadeout", "head", "tail") if setting(project, k) not in ("0", "")]
    if any(item["on"] for item in master_track(project)["fx"]):
        master_bits.insert(0, "effects")
    lufs = setting(project, "lufs")
    if master_bits or lufs != "off":
        what = ", ".join(master_bits + (["the loudness target"] if lufs != "off" else []))
        print(f"      not in the stems: master {what}. Summed at unity they give the mix before the master step.")
    else:
        print(f"      summed at unity they give {MASTER_WAV} exactly")


def apply_document(project: Project, data: dict, settings: bool = True, tracks: bool = True,
                   verbose: bool = False) -> tuple[int, int, list[str]]:
    """Apply a gout.json document to the project. Returns (settings, tracks applied, warnings)."""
    warnings: list[str] = []
    n_settings = 0
    if settings:
        master = (data.get("master") or {}).get("fx")
        if master is None:
            master = legacy_chain(data.get("project") or {}) or None
        if master is not None:
            apply_chain(project, MASTER_OWNER, "master", master, warnings)
            n_settings += 1
        for key, value in (data.get("project") or {}).items():
            if key in ("name", "created") or key.startswith(("master_", "ui_")):
                continue
            if key == "automix":
                key = "autorender"
            if key == "autorender" and value == "on" and "settings_version" not in (data.get("project") or {}):
                value = "idle"  # a document from before render modes, where on was only the default
            if key not in ("rate", "autorender") and key not in MASTER_DEFAULTS:
                continue  # something a newer or older gout knew about
            try:
                k, v = parse_setting(key, str(value))
            except GoutError as exc:
                warnings.append(f"{key}: {exc}")
                continue
            project.set(k, v)
            n_settings += 1
    n_tracks = 0
    if tracks:
        order: list[str] = []
        for item in data.get("tracks") or []:
            if not isinstance(item, dict):
                continue
            file, name = item.get("file"), item.get("name")
            current = project.tracks()
            t = next((x for x in current if x["file"] == file), None) or \
                next((x for x in current if x["name"] == name), None)
            if t is None:
                path = project.tracks_dir / file if file else None
                if path is not None and path.is_file():
                    t, _ = ingest(project, path, name, 0, verbose)
                else:
                    warnings.append(f"{name or file}: no such file in {TRACK_DIR}/, skipped")
                    continue
            fields: dict = {}
            try:
                if item.get("offset_ms") is not None:
                    fields["offset_ms"] = int(item["offset_ms"])
                if item.get("in_ms") is not None:
                    fields["in_ms"] = max(0, min(int(item["in_ms"]), t["length_ms"] - 1))
                if "out_ms" in item:
                    out = item["out_ms"]
                    fields["out_ms"] = None if out is None or int(out) >= t["length_ms"] else int(out)
                if item.get("gain_db") is not None:
                    fields["gain_db"] = max(-60.0, min(24.0, float(item["gain_db"])))
                if item.get("pan") is not None:
                    fields["pan"] = max(-1.0, min(1.0, float(item["pan"])))
                for flag in ("mute", "solo"):
                    if item.get(flag) is not None:
                        fields[flag] = 1 if item[flag] else 0
            except (TypeError, ValueError) as exc:
                warnings.append(f"{t['name']}: bad value ({exc}), skipped")
                continue
            in_ms = fields.get("in_ms", t["in_ms"])
            out_ms = fields.get("out_ms", t["out_ms"])
            if out_ms is not None and out_ms <= in_ms:
                fields["out_ms"] = None
            if fields:
                project.update(t["n"], **fields)
            if isinstance(item.get("fx"), list):
                apply_chain(project, t["file"], t["name"], item["fx"], warnings)
            elif any(item.get(kind) is not None for kind in ("eq", "comp", "delay", "reverb")):
                apply_chain(project, t["file"], t["name"], legacy_chain(item), warnings)
            order.append(t["file"])
            n_tracks += 1
        if order:
            project.reorder(order)
    return n_settings, n_tracks, warnings


def read_document(path: Path) -> dict:
    if not path.is_file():
        die(f"no such file: {path}")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or not ("project" in data or "tracks" in data):
        die(f"{path} is not a gout document (expected the shape of gout dump)")
    return data


def cmd_import(project: Project, args: Args) -> None:
    only_settings = args.flag("--settings", "-s")
    only_tracks = args.flag("--tracks", "-t")
    (path,) = args.positionals("gout import FILE.json [-s | -t]   (-s settings only, -t tracks only)", 1, 1)
    data = read_document(Path(path).expanduser())
    project.record(f"import {Path(path).name}")
    n_settings, n_tracks, warnings = apply_document(project, data, settings=not only_tracks,
                                                    tracks=not only_settings, verbose=args.verbose)
    print(f"import {path}  {n_settings} setting{'' if n_settings == 1 else 's'},"
          f" {n_tracks} track{'' if n_tracks == 1 else 's'}")
    for w in warnings:
        print(f"      {w}")
    autorender(project, args)


def cmd_undo(project: Project, args: Args) -> None:
    args.positionals("gout undo")
    command = project.undo()
    print(f"undo  {command}")
    autorender(project, args)


def save_as(project: Project, name: str) -> Project:
    """Copy the whole project (database, master/, renders) to a new directory and open it.

    A bare name lands next to the current project; a path with a slash goes where it says.
    """
    dst = Path(name).expanduser()
    if not dst.is_absolute() and "/" not in name:
        dst = project.root.parent / name
    dst = dst.resolve()
    if dst == project.root:
        die("that is this project")
    if dst.exists():
        die(f"{dst} already exists")
    if project.root in dst.parents:
        die("the copy would end up inside this project — give a name for a sibling, or a path")
    dst.mkdir(parents=True)
    shutil.copy2(project.db_path, dst / DB_NAME)
    shutil.copytree(project.tracks_dir, dst / TRACK_DIR)
    for extra in (MASTER_WAV, MASTER_MP3):
        if (project.root / extra).exists():
            shutil.copy2(project.root / extra, dst / extra)
    copy = Project(dst)
    copy.set("name", dst.name)
    copy.set("created", dt.datetime.now().isoformat(timespec="seconds"))
    copy.sync_json()
    return copy


def cmd_saveas(project: Project, args: Args) -> None:
    (name,) = args.positionals("gout saveas NAME | PATH   (a bare name goes next to this project)", 1, 1)
    copy = save_as(project, name)
    files = sum(1 for _ in copy.tracks_dir.iterdir())
    print(f"saved {copy.root}  ({files} track file{'' if files == 1 else 's'} copied; this project is untouched)")


def cmd_dump(project: Project, args: Args) -> None:
    args.positionals("gout dump")
    print(json.dumps(project.document(), indent=2))


def cmd_rebuild(root_hint: Path | None, args: Args) -> None:
    force = args.flag("-f", "--force")
    args.positionals("gout rebuild [-f]   (run inside the project, or gout -p DIR rebuild)")
    root = (root_hint or Path.cwd()).resolve()
    tracks_dir = root / TRACK_DIR
    if not tracks_dir.is_dir():
        die(f"no {TRACK_DIR}/ directory in {root}")
    db = root / DB_NAME
    if db.exists():
        if not force:
            die(f"{db} exists — pass -f to replace it")
        db.unlink()
    project = Project.create(root, DEFAULT_RATE)
    files = sorted(p for p in tracks_dir.iterdir() if p.suffix.lower() in (".wav", ".mp3"))
    print(f"rebuild  {root}  ({len(files)} files in {TRACK_DIR}/)")
    for path in files:
        ingest(project, path, None, 0, args.verbose)
    side = root / SIDECAR
    if side.is_file():
        n_settings, n_tracks, warnings = apply_document(project, read_document(side), verbose=args.verbose)
        print(f"      positions, trims and settings restored from {SIDECAR}"
              f" ({n_settings} settings, {n_tracks} tracks)")
        for w in warnings:
            print(f"      {w}")
    else:
        print(f"      no {SIDECAR} found: every track at 0, default settings")
    project.sync_json()
    if files and project.render_mode != "off":
        mix(project)


def cmd_view(project: Project, args: Args) -> None:
    width_txt = args.value("-w", "--width")
    args.positionals("gout view [-w COLUMNS]")
    width = int(width_txt) if width_txt and width_txt.isdigit() else shutil.get_terminal_size((100, 24)).columns
    tracks = project.tracks()
    master = project.get("master_ms")
    state = (f"{MASTER_WAV} {fmt_ms(int(master))}" if master and project.master.exists()
             else f"{MASTER_WAV} not rendered")
    print(f"proj  {project.get('name')}  {project.rate} Hz  {len(tracks)} track"
          f"{'' if len(tracks) == 1 else 's'}  {state}")
    for label, cells, _, _, _ in render_timeline(project, width):
        print(f"{label:<{LABEL_W}} {cells}".rstrip())


def cmd_cheat(root_hint: Path | None, args: Args) -> None:
    width_txt = args.value("-w", "--width")
    args.positionals("gout cheat [-w COLUMNS]")
    width = int(width_txt) if width_txt and width_txt.isdigit() else shutil.get_terminal_size((100, 24)).columns
    print("\n".join(render_cheat(width)))


def cmd_ui(project: Project, args: Args) -> None:
    args.positionals("gout ui")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die("the ui needs a terminal")
    run_tui(project)


def run_tui(project) -> None:
    """The ui sits above the commands; import it only when it is wanted."""
    from .tui import run_tui as start
    start(project)


def cmd_addons(root_hint: Path | None, args: Args) -> None:
    """Where addons are read from, what loaded, and what the project here uses but lacks."""
    from .addons import addon_dir, REPORT
    args.positionals("gout addons")
    effects()
    folder = addon_dir()
    state = "" if folder.is_dir() else "  (does not exist yet: mkdir -p it and copy addons in)"
    print(f"addons {folder}{state}")
    if not REPORT and folder.is_dir():
        print("       no addon files there; examples/addons/tremolo.py in the gout checkout is one to copy")
    for entry in REPORT:
        if entry["error"]:
            print(f"  {entry['file'].name:<24} not loaded: {entry['error']}")
        else:
            from .screens import screens
            added = entry["effects"] + [f"{name} (screen{', ' + screens()[name].key if screens()[name].key else ''})"
                                        for name in entry.get("screens", [])]
            print(f"  {entry['file'].name:<24} {', '.join(added) or 'registered nothing'}")
    project = Project.find(root_hint)
    if project is not None:
        used = {item["kind"] for t in project.tracks() + [master_track(project)] for item in t["fx"]}
        missing = sorted(kind for kind in used if effect(kind) is None)
        if missing:
            print(f"       this project uses effects that are not installed: {', '.join(missing)}")


def cmd_colors(root_hint: Path | None, args: Args) -> None:
    """Which color.json is in use, what is wrong with it, and every setting with its value."""
    from .theme import DEFAULTS, default_document, HELP, load_theme, user_file, FILE_NAME
    init = args.flag("--init", "-i")
    here = args.flag("--project", "-P")
    force = args.flag("-f", "--force")
    args.positionals("gout colors [--init [--project] [-f]]   (--init writes the defaults to fill in)")
    project = Project.find(root_hint)
    if init:
        if here and project is None:
            die("--project needs a project here or above")
        target = project.root / FILE_NAME if here else user_file()
        if target.exists() and not force:
            die(f"{target} already exists — pass -f to replace it with the defaults")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(default_document())
        print(f"colors wrote {target}  ({len(DEFAULTS)} settings, each explained under _help)")
        return
    theme, problems, path = load_theme(project.root if project else None)
    where = str(path) if path else f"none: the defaults (gout colors --init writes {user_file()})"
    print(f"colors {where}")
    if project is not None and path != project.root / FILE_NAME:
        print(f"       a {FILE_NAME} in {project.root} would take precedence for this project")
    for problem in problems:
        print(f"  problem: {problem}")
    width = max(len(k) for k in DEFAULTS)
    for key in DEFAULTS:
        value = json.dumps(theme[key], ensure_ascii=False)
        mark = " " if theme[key] == DEFAULTS[key] else "*"
        print(f" {mark}{key:<{width}}  {value:<36} {HELP[key]}")
