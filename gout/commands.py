"""The project commands behind gout's subcommands and the ui prompt."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

from .core import (
    DB_NAME,
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
from .effects.eq import EQ_PRESETS, EQ_SLOPES, EQ_SYNTAX, fmt_eq, parse_eq, parse_hz, render_eq
from .effects.comp import COMP_PRESETS, COMP_SYNTAX, fmt_comp, parse_comp, render_comp
from .effects.delay import DELAY_PRESETS, DELAY_SYNTAX, fmt_delay, parse_delay, render_delay
from .effects.reverb import (
    fmt_reverb,
    impulse_path,
    parse_reverb,
    render_reverb,
    REVERB_PRESETS,
    REVERB_SYNTAX,
)
from .settings import BITS_CODEC, MASTER_DEFAULTS, master_track, parse_setting, project_bpm, setting, TAG_KEYS
from .project import Project
from .mixer import autorender, mix, sounding_end, track_chain, track_steps
from .render import LABEL_W, render_cheat, render_timeline


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
        die(f"no such file: {src}")
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


def cmd_add(project: Project, args: Args) -> None:
    name = args.value("--name", "-n")
    at = args.value("--at", "-a")
    files = args.positionals("gout add FILE... [-n NAME] [-a TIME] [-N]", 1)
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
          f"{'' if len(tracks) == 1 else 's'}  {TRACK_DIR}/  autorender {'on' if project.autorender else 'off'}")
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
        if t["eq"]:
            flags += f"  eq {t['eq']}" + ("" if t["eq_on"] else " (off)")
        if t["comp"]:
            flags += f"  comp {t['comp']}" + ("" if t["comp_on"] else " (off)")
        if t["delay"]:
            flags += f"  delay {t['delay']}" + ("" if t["delay_on"] else " (off)")
        if t["reverb"]:
            flags += f"  reverb {t['reverb']}" + ("" if t["reverb_on"] else " (off)")
        print(f"{t['n']:>4}  {t['name']:<16} {t['kind']:<4} {t['channels']:>2}  {fmt_ms(start):<12} "
              f"{fmt_ms(b - a):<12} {fmt_ms(t['length_ms']):<12} {trim:<27} "
              f"{fmt_db(t['gain_db']):<7} {fmt_pan(t['pan']):<4} {flags}")
    master_ms = project.get("master_ms")
    if master_ms and project.master.exists():
        lufs, tp = project.get("master_lufs"), project.get("master_tp")
        loud = f"  {float(lufs):.1f} LUFS  peak {float(tp):+.1f} dBTP" if lufs and tp else ""
        print(f"      {MASTER_WAV}  {fmt_ms(int(master_ms))}{loud}")
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


EQ_USAGE = (f"gout eq TRACK [BANDS... | PRESET | on | off | clear]\n       bands: {EQ_SYNTAX}\n"
            f"       presets: {' '.join(EQ_PRESETS)}   (eq presets explains them)\n"
            "       hp/lp: cut with a slope in dB per octave (12 by default); +3@200: a peak of +3 dB at 200 Hz,\n"
            "       /3 sets its Q; ls100:+2 and hs8k:-3 are shelves")


def eq_line(t: dict) -> str:
    text = t["eq"] or "flat"
    if t["eq"] and not t["eq_on"]:
        text += "  (off: bypassed, eq TRACK on brings it back)"
    return f"eq    {t['n']:>2}  {t['name']:<16} {text}"


def cmd_eq(project: Project, args: Args) -> None:
    pos = args.positionals(EQ_USAGE, 1)
    if pos[0].lower() in ("presets", "preset", "list"):
        print("presets  a name stands for these bands; use it alone or with bands of your own, eq 3 voice +1@5k")
        for name, (bands, what) in EQ_PRESETS.items():
            print(f"  {name:<8} {bands or 'flat':<38} {what}")
        return
    master = is_master(pos[0])
    t = master_track(project) if master else project.track(pos[0])
    words = pos[1:]
    if master and words:
        key, value = parse_setting("eq", " ".join(words) if words not in (["on"],) else t["eq"])
        project.record(f"set eq {value}")
        project.set("eq", value)
        print(eq_line(master_track(project)))
        autorender(project, args)
        return
    if not words:
        print(eq_line(t))
        width = min(100, shutil.get_terminal_size((100, 24)).columns)
        path = project.master if master else project.tracks_dir / t["file"]
        spectrum = (project.spectrum(t["file"], path) if path.exists() else b"") or None
        for text, _, kind in render_eq(project, t, width - 6, spectrum=spectrum)[1:]:
            print("      " + text)
        if spectrum:
            print("      ░ the track's own spectrum, loudest band at the top")
        return
    if words == ["off"]:
        project.record(f"eq {t['name']} off")
        project.update(t["n"], eq_on=0)
    elif words == ["on"]:
        project.record(f"eq {t['name']} on")
        project.update(t["n"], eq_on=1)
    elif words == ["clear"]:
        project.record(f"eq {t['name']} clear")
        project.update(t["n"], eq="", eq_on=1)
    else:
        try:
            bands = parse_eq(" ".join(words))
        except ValueError as exc:
            die(f"{exc}\n{EQ_USAGE}")
        project.record(f"eq {t['name']} {' '.join(words)}")
        project.update(t["n"], eq=fmt_eq(bands), eq_on=1)
    print(eq_line(project.track(str(t["n"]))))
    autorender(project, args)


COMP_USAGE = (f"gout comp TRACK [SETTINGS... | PRESET | on | off | clear]\n       settings: {COMP_SYNTAX}\n"
              f"       presets: {' '.join(COMP_PRESETS)}   (comp presets explains them)")


def comp_line(t: dict) -> str:
    text = t["comp"] or "none"
    if t["comp"] and not t["comp_on"]:
        text += "  (off: bypassed, comp TRACK on brings it back)"
    return f"comp  {t['n']:>2}  {t['name']:<16} {text}"


def cmd_comp(project: Project, args: Args) -> None:
    pos = args.positionals(COMP_USAGE, 1)
    if pos[0].lower() in ("presets", "preset", "list"):
        print("presets  a name stands for these settings; add your own after it, comp 3 vocal a10")
        for name, (line, what) in COMP_PRESETS.items():
            print(f"  {name:<8} {line or 'none':<26} {what}")
        return
    master = is_master(pos[0])
    t = master_track(project) if master else project.track(pos[0])
    words = pos[1:]
    if master and words:
        key, value = parse_setting("comp", " ".join(words) if words != ["on"] else t["comp"])
        project.record(f"set comp {value}")
        project.set("comp", value)
        print(comp_line(master_track(project)))
        autorender(project, args)
        return
    if not words:
        print(comp_line(t))
        width = min(100, shutil.get_terminal_size((100, 24)).columns)
        path = project.master if master else project.tracks_dir / t["file"]
        peaks = (project.envelope(t["file"], path) if path.exists() else b"") or None
        for text, _, kind in render_comp(t, min(width - 6, 64), peaks=peaks):
            print("      " + text)
        print("      · output = input   █ the compressor   ░ how often this track's peaks sit at that level")
        return
    if words == ["off"]:
        project.record(f"comp {t['name']} off")
        project.update(t["n"], comp_on=0)
    elif words == ["on"]:
        project.record(f"comp {t['name']} on")
        project.update(t["n"], comp_on=1)
    elif words in (["clear"], ["none"]):
        project.record(f"comp {t['name']} clear")
        project.update(t["n"], comp="", comp_on=1)
    else:
        try:
            c = parse_comp(" ".join(words))
        except ValueError as exc:
            die(f"{exc}\n{COMP_USAGE}")
        project.record(f"comp {t['name']} {' '.join(words)}")
        project.update(t["n"], comp=fmt_comp(c), comp_on=1)
    print(comp_line(project.track(str(t["n"]))))
    autorender(project, args)


DELAY_USAGE = (f"gout delay TRACK [SETTINGS... | PRESET | on | off | clear]\n       settings: {DELAY_SYNTAX}\n"
               f"       presets: {' '.join(DELAY_PRESETS)}   (delay presets explains them)")


def delay_line(t: dict) -> str:
    text = t["delay"] or "none"
    if t["delay"] and not t["delay_on"]:
        text += "  (off: bypassed, delay TRACK on brings it back)"
    return f"delay {t['n']:>2}  {t['name']:<16} {text}"


def cmd_delay(project: Project, args: Args) -> None:
    pos = args.positionals(DELAY_USAGE, 1)
    if pos[0].lower() in ("presets", "preset", "list"):
        print("presets  a name stands for these settings; add your own after it, delay 3 slap w40")
        for name, (line, what) in DELAY_PRESETS.items():
            print(f"  {name:<8} {line or 'none':<20} {what}")
        return
    master = is_master(pos[0])
    t = master_track(project) if master else project.track(pos[0])
    words = pos[1:]
    if master and words:
        key, value = parse_setting("delay", " ".join(words) if words != ["on"] else t["delay"])
        project.record(f"set delay {value}")
        project.set("delay", value)
        print(delay_line(master_track(project)))
        autorender(project, args)
        return
    if not words:
        print(delay_line(t))
        width = min(100, shutil.get_terminal_size((100, 24)).columns)
        for text, _, kind in render_delay(t, project_bpm(project), min(width - 6, 70)):
            print("      " + text)
        return
    if words == ["off"]:
        project.record(f"delay {t['name']} off")
        project.update(t["n"], delay_on=0)
    elif words == ["on"]:
        project.record(f"delay {t['name']} on")
        project.update(t["n"], delay_on=1)
    elif words in (["clear"], ["none"]):
        project.record(f"delay {t['name']} clear")
        project.update(t["n"], delay="", delay_on=1)
    else:
        try:
            d = parse_delay(" ".join(words))
        except ValueError as exc:
            die(f"{exc}\n{DELAY_USAGE}")
        if not d["time"].endswith("ms") and project_bpm(project) is None:
            die(f"{d['time']} is a note value: set bpm 120 first, or give the time in ms")
        project.record(f"delay {t['name']} {' '.join(words)}")
        project.update(t["n"], delay=fmt_delay(d), delay_on=1)
    print(delay_line(project.track(str(t["n"]))))
    autorender(project, args)


REVERB_USAGE = (f"gout reverb TRACK [SETTINGS... | PRESET | on | off | clear]\n       settings: {REVERB_SYNTAX}\n"
                f"       presets: {' '.join(REVERB_PRESETS)}   (reverb presets explains them)")


def reverb_line(t: dict) -> str:
    text = t["reverb"] or "none"
    if t["reverb"] and not t["reverb_on"]:
        text += "  (off: bypassed, reverb TRACK on brings it back)"
    return f"reverb {t['n']:>1}  {t['name']:<16} {text}"


def cmd_reverb(project: Project, args: Args) -> None:
    pos = args.positionals(REVERB_USAGE, 1)
    if pos[0].lower() in ("presets", "preset", "list"):
        print("presets  a name stands for these settings; add your own after it, reverb 3 hall w15")
        for name, (line, what) in REVERB_PRESETS.items():
            print(f"  {name:<10} {line or 'none':<18} {what}")
        return
    master = is_master(pos[0])
    t = master_track(project) if master else project.track(pos[0])
    words = pos[1:]
    if master and words:
        key, value = parse_setting("reverb", " ".join(words) if words != ["on"] else t["reverb"])
        project.record(f"set reverb {value}")
        project.set("reverb", value)
        print(reverb_line(master_track(project)))
        autorender(project, args)
        return
    if not words:
        print(reverb_line(t))
        width = min(100, shutil.get_terminal_size((100, 24)).columns)
        for text, _, kind in render_reverb(project, t, min(width - 6, 70)):
            print("      " + text)
        return
    if words == ["off"]:
        project.record(f"reverb {t['name']} off")
        project.update(t["n"], reverb_on=0)
    elif words == ["on"]:
        project.record(f"reverb {t['name']} on")
        project.update(t["n"], reverb_on=1)
    elif words in (["clear"], ["none"]):
        project.record(f"reverb {t['name']} clear")
        project.update(t["n"], reverb="", reverb_on=1)
    else:
        try:
            r = parse_reverb(" ".join(words))
        except ValueError as exc:
            die(f"{exc}\n{REVERB_USAGE}")
        project.record(f"reverb {t['name']} {' '.join(words)}")
        project.update(t["n"], reverb=fmt_reverb(r), reverb_on=1)
        impulse_path(project, r)  # synthesise now, so the mix does not pause on it
    print(reverb_line(project.track(str(t["n"]))))
    autorender(project, args)


def _cut(project: Project, args: Args, kind: str) -> None:
    pos = args.positionals(f"gout {kind} TRACK HZ [SLOPE] | off     e.g. {kind} 3 {'80' if kind == 'hp' else '12k'}"
                           f"  ({kind} 3 80 24 for 24 dB per octave)", 2, 3)
    t = project.track(pos[0])
    try:
        bands = [b for b in parse_eq(t["eq"]) if b["type"] != kind]
        if pos[1].lower() != "off":
            slope = int(pos[2]) if len(pos) > 2 else 12
            if slope not in EQ_SLOPES:
                raise ValueError(f"slope {pos[2]}: use 6, 12, 18, 24, 36 or 48 dB per octave")
            bands.append({"type": kind, "f": parse_hz(pos[1]), "slope": slope})
        bands = parse_eq(fmt_eq(bands))  # canonical order
    except ValueError as exc:
        die(str(exc))
    project.record(f"{kind} {t['name']} {' '.join(pos[1:])}")
    project.update(t["n"], eq=fmt_eq(bands), eq_on=1)
    print(eq_line(project.track(str(t["n"]))))
    autorender(project, args)


def cmd_hp(project: Project, args: Args) -> None:
    _cut(project, args, "hp")


def cmd_lp(project: Project, args: Args) -> None:
    _cut(project, args, "lp")


def cmd_set(project: Project, args: Args) -> None:
    pos = args.positionals(SET_USAGE, 0)
    if not pos:
        hints = {
            "autorender": "render master.wav after every change",
            "lufs": "loudness target: -14 (streaming) -16 (Apple) -23 (broadcast) or off",
            "ceiling": "true-peak ceiling in dBTP for the loudness step",
            "eq": "master eq, same syntax as a track's: hp30 hs10k:+1, or a preset",
            "comp": "master compressor: -16 2:1 a30 r300 k8, or a preset like glue",
            "delay": "master delay: 1/8 w20 f30 n3, or a preset like slap",
            "reverb": "master reverb: 2.5s p20 d50 w15, or a preset like hall",
            "bpm": "tempo, so delays can be note values like 1/8",
            "gain": "master gain in dB, before the loudness step",
            "fadein": "e.g. 500ms", "fadeout": "e.g. 3s", "head": "silence before, e.g. 500ms",
            "tail": "silence after, e.g. 2s", "bits": "32f | 24 | 16 (dithered)",
            "mp3": "bounce quality: 320k, 192k, v0 .. v9",
        }
        print(f"{'name':<11} {project.get('name')}")
        print(f"{'rate':<11} {project.rate}")
        print(f"{'autorender':<11} {'on' if project.autorender else 'off':<14} {hints['autorender']}")
        for key in MASTER_DEFAULTS:
            value = setting(project, key)
            if key in ("fadein", "fadeout", "head", "tail") and value != "0":
                value = fmt_ms(int(value))
            elif key in TAG_KEYS:
                value = value or "-"
            elif key in ("eq", "comp", "delay", "reverb"):
                value = value or "none"
            elif key == "bpm":
                value = value or "-"
            print(f"{key:<11} {value:<14} {hints.get(key, '')}")
        return
    if len(pos) < 2 or (len(pos) > 2 and pos[0].lower() not in ("eq", "comp", "delay", "reverb") + TAG_KEYS):
        die(SET_USAGE)
    key, value = parse_setting(pos[0].lower(), " ".join(pos[1:]))  # eq, comp and tags may span words
    if key == "bpm" and not value:
        users = [t["name"] for t in project.tracks() + [master_track(project)]
                 if t["delay"] and not t["delay"].split()[0].endswith("ms")]
        if users:
            die(f"cannot clear bpm: the delay on {', '.join(users)} uses a note value; give it a time in ms first")
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
        steps = track_steps(project, t)
        if steps:
            plans.append((t, steps))
    if not plans:
        die("nothing to export: no track has audible material" + (" the mix hears" if audible_only else ""))
    out_dir = Path(pos[0]).expanduser() if pos else project.root / STEMS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    end_ms = max(1, max(sounding_end(project, t) for t, _ in plans))
    bits = setting(project, "bits")
    print(f"stems {out_dir}  {len(plans)} track{'' if len(plans) == 1 else 's'}, {fmt_ms(end_ms)} each"
          f"  ({bits}, {project.rate} Hz, stereo)")
    for t, steps in plans:
        name = f"{t['n']:02d}-{t['name']}.wav"
        inputs = [project.tracks_dir / t["file"]]
        graph = track_chain(project, t, steps, "[0:a]", "s", inputs) + f";[s]apad=whole_dur={end_ms / 1000:.3f}[out]"
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
    master_bits = [k for k in ("eq", "comp", "delay", "reverb", "gain", "fadein", "fadeout", "head", "tail")
                   if setting(project, k) not in ("0", "")]
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
        for key, value in (data.get("project") or {}).items():
            if key in ("name", "created") or key.startswith(("master_", "ui_")):
                continue
            if key == "automix":
                key = "autorender"
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
                for flag in ("mute", "solo", "eq_on", "comp_on", "delay_on", "reverb_on"):
                    if item.get(flag) is not None:
                        fields[flag] = 1 if item[flag] else 0
                if item.get("eq") is not None:
                    try:
                        fields["eq"] = fmt_eq(parse_eq(str(item["eq"])))
                    except ValueError as exc:
                        warnings.append(f"{t['name']}: eq ignored ({exc})")
                if item.get("comp") is not None:
                    try:
                        fields["comp"] = fmt_comp(parse_comp(str(item["comp"]))) if str(item["comp"]) else ""
                    except ValueError as exc:
                        warnings.append(f"{t['name']}: comp ignored ({exc})")
                if item.get("delay") is not None:
                    try:
                        fields["delay"] = fmt_delay(parse_delay(str(item["delay"]))) if str(item["delay"]) else ""
                    except ValueError as exc:
                        warnings.append(f"{t['name']}: delay ignored ({exc})")
                if item.get("reverb") is not None:
                    try:
                        fields["reverb"] = fmt_reverb(parse_reverb(str(item["reverb"]))) if str(item["reverb"]) else ""
                    except ValueError as exc:
                        warnings.append(f"{t['name']}: reverb ignored ({exc})")
            except (TypeError, ValueError) as exc:
                warnings.append(f"{t['name']}: bad value ({exc}), skipped")
                continue
            in_ms = fields.get("in_ms", t["in_ms"])
            out_ms = fields.get("out_ms", t["out_ms"])
            if out_ms is not None and out_ms <= in_ms:
                fields["out_ms"] = None
            if fields:
                project.update(t["n"], **fields)
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
    if files and project.autorender:
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
    for label, cells, _, _ in render_timeline(project, width):
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
