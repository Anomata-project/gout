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
    resource_dir,
    run_quiet,
    SIDECAR,
    STEMS_DIR,
    TRACK_DIR,
)
from .media import cut, mp3_frame_cut, probe
from .model import (audible, is_heard, meets, MIN_PART_MS, part_has_settings, part_label, part_owner, part_settings, part_start,
                    PART_WORD, timeline)
from .fx import Effect, effect, effects, FxContext, resolve
from .settings import BITS_CODEC, MASTER_DEFAULTS, master_track, parse_setting, setting, TAG_KEYS
from .project import legacy_chain, Project
from .mixer import autorender, live_source, mix, part_as_owner, sounding_end, sounds, track_chain
from .render import effect_picture, LABEL_W, render_cheat, render_timeline
from .player import Player
from .recorder import (choose_backend, CLIP, current_input, default_output, find_input, input_is_muted, level_db,
                       list_inputs, load_settings, meter, Monitor, monitor_plan, Recorder, save_settings)
from .engine import (calibration, CLICK_TIMES, click_offsets, duplex_available, Engine, install_hint, output_for,
                     save_calibration)


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


def ingest(project: Project, src: Path, name: str | None, at_ms: int, verbose: bool,
           verb: str = "add") -> tuple[dict, bool]:
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
    print(f"{verb:<5} {track['n']:>2}  {track['name']:<16} {kind}  {info['channels']}ch  {info['sample_rate']} Hz"
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
        start, end = timeline(t)
        trimmed = a > 0 or b < t["length_ms"]
        trim = f"{fmt_ms(a)} > {fmt_ms(b)}" if trimmed else "-"
        flags = ("M" if t["mute"] else "-") + ("S" if t["solo"] else "-")
        if not is_heard(t, any_solo):
            flags += " (silent)"
        if t["fx"]:
            flags += "  fx " + " | ".join(fx_text(item) for item in t["fx"])
        print(f"{t['n']:>4}  {t['name']:<16} {t['kind']:<4} {t['channels']:>2}  {fmt_ms(start):<12} "
              f"{fmt_ms(end - start):<12} {fmt_ms(t['length_ms']):<12} {trim:<27} "
              f"{fmt_db(t['gain_db']):<7} {fmt_pan(t['pan']):<4} {flags}")
        for part in t["parts"]:
            begin = part_start(t, part)
            print(f"{'':>4}    {part_text(t['parts'], part):<14} {'':<4} {'':>2}  {fmt_ms(begin):<12} "
                  f"{fmt_ms(part['out_ms'] - part['in_ms']):<12} {'':<12} "
                  f"{fmt_ms(part['in_ms']) + ' > ' + fmt_ms(part['out_ms']):<27} "
                  f"{fmt_db(part['gain_db']):<7} {fmt_pan(part['pan']):<4} {'M' if part['mute'] else '-'}"
                  + ("  fx " + " | ".join(fx_text(item) for item in part["fx"]) if part.get("fx") else ""))
    master_ms = project.get("master_ms")
    if master_ms and project.master.exists():
        lufs, tp = project.get("master_lufs"), project.get("master_tp")
        loud = f"  {float(lufs):.1f} LUFS  peak {float(tp):+.1f} dBTP" if lufs and tp else ""
        stale = "" if project.master_is_current() else "  (out of date: play streams the project live, mix renders it)"
        print(f"      {MASTER_WAV}  {fmt_ms(int(master_ms))}{loud}{stale}")
    else:
        print(f"      {MASTER_WAV} not rendered — gout mix")


MOVE_USAGE = "gout move TRACK [PART]... | all +TIME | -TIME | TIME  (or =TIME)"


def cmd_move(project: Project, args: Args) -> None:
    """One track, several (move 1 3 +2s) or all of them by the same amount. A time without a sign
    places the earliest audible start there and keeps the spacing. One undo puts them all back."""
    words = args.positionals(MOVE_USAGE, 2)
    specs, delta = words[:-1], words[-1]
    parts_of: dict[int, list[dict]] = {}  # track number -> the parts named after it, when not all of it moves
    if "all" in specs:
        chosen = project.tracks()
        if not chosen:
            die("the project has no tracks yet — gout add FILE")
    else:
        chosen = []
        for spec in specs:
            last = chosen[-1] if chosen else None
            part = find_part(last, spec) if last is not None else None
            if part is not None:  # a part of the track just named
                parts_of.setdefault(last["n"], []).append(part)
                continue
            if last is not None and last["parts"] and re.fullmatch(rf"{PART_WORD}\d+", spec.lower()):
                pick_part(last, spec)  # says which parts there are
            try:
                chosen.append(project.track(spec))
            except GoutError as exc:
                die(f"{exc}\nusage: {MOVE_USAGE}")
    chosen = list({t["n"]: t for t in chosen}.values())  # a track named twice moves once
    whole = [t for t in chosen if t["n"] not in parts_of]
    pieces = [(t, part) for t in chosen for part in {p["id"]: p for p in parts_of.get(t["n"], [])}.values()]
    starts = [timeline(t)[0] for t in whole] + [part_start(t, part) for t, part in pieces]
    if delta[0] in "+-":
        step = parse_ms(delta[1:])
        shift = step if delta[0] == "+" else -step
    else:
        text = delta[1:] if delta[0] == "=" else delta
        at = parse_ms(text.lstrip("+-"))
        shift = (-at if text.startswith("-") else at) - min(starts)
    project.record(f"move {'all' if 'all' in specs else ' '.join(specs)} {delta}")
    for t in whole:
        offset = t["offset_ms"] + shift
        project.update(t["n"], offset_ms=offset)
        start, end = timeline({**t, "offset_ms": offset})
        cut = f"  (before 0:00: its first {-start / 1000:g} s is not heard)" if start < 0 else ""
        print(f"move  {t['n']:>2}  {t['name']:<16} at {fmt_ms(start)} -> {fmt_ms(end)}{cut}")
    for t, part in pieces:
        project.part_update(part["id"], shift_ms=part["shift_ms"] + shift)
        start = part_start(t, part) + shift
        cut = f"  (before 0:00: its first {-start / 1000:g} s is not heard)" if start < 0 else ""
        print(f"move  {t['n']:>2}  {t['name']:<16} {part_label(t['parts'], part):<8} at {fmt_ms(start)} -> "
              f"{fmt_ms(start + part['out_ms'] - part['in_ms'])}{cut}")
    autorender(project, args)


TRIM_USAGE = ("gout trim TRACK [-st TIME] [-et TIME | -el TIME] [-c]          soft: file untouched\n"
              "       gout trim TRACK -H [-st ..] [-et ..|-el ..] [-r]           hard: rewrite the file\n"
              "       times are measured from the start of the track's own file; -5s is 5 s before its end\n"
              "       gout trim TRACK PART [-st TIME] [-et TIME]                 a part: where you hear it start or\n"
              "       end; +200ms or -1s moves that edge from where it is")


def cmd_trim(project: Project, args: Args) -> None:
    hard = args.flag("--hard", "-H")
    clear = args.flag("--clear", "-c")
    reencode = args.flag("-r", "--reencode")
    st, et, el = args.value("-st", "--start"), args.value("-et", "--end"), args.value("-el", "--length")
    words = args.positionals(TRIM_USAGE, 1, 2)
    if et and el:
        die("-et and -el are exclusive\n" + TRIM_USAGE)
    t = project.track(words[0])
    length = t["length_ms"]
    if len(words) == 2:
        if hard or clear or el:
            die("a part takes -st and -et: where it is heard to start and end (or +/- to move an edge)\n" + TRIM_USAGE)
        trim_part(project, t, pick_part(t, words[1]), st, et)
        autorender(project, args)
        return

    def point(text: str) -> int:
        """A moment in the file: from its start, or with a minus back from its end."""
        return length - parse_ms(text[1:]) if text.startswith("-") else parse_ms(text)

    a, b = audible(t)
    if clear:
        a, b = 0, length
    if st:
        a = point(st)
    if et:
        b = point(et)
    elif el:
        b = a + parse_ms(el)
    b = min(b, length)
    if a < 0 or a >= length:
        die(f"in point {fmt_ms(a)} is outside the file ({fmt_ms(length)} long)")
    if b <= a:
        die(f"out point {fmt_ms(b)} is not after the in point {fmt_ms(a)}")

    parts = t["parts"]
    if parts and hard:
        die(f"track {t['n']} {t['name']} is in parts; a hard trim needs one piece: gout part {t['n']} join first")
    if parts and (clear or st or et or el):  # the outer ends are the first part's start and the last part's end
        first, last = parts[0], parts[-1]
        if a != first["in_ms"] and (len(parts) > 1 and a > first["out_ms"] - MIN_PART_MS):
            die(f"in point {fmt_ms(a)} is past the end of {part_label(parts, first)} ({fmt_ms(first['out_ms'])} in the file)")
        if b != last["out_ms"] and (len(parts) > 1 and b < last["in_ms"] + MIN_PART_MS):
            die(f"out point {fmt_ms(b)} is before the start of {part_label(parts, last)} ({fmt_ms(last['in_ms'])} in the file)")

    if not (hard or clear or st or et or el):
        trimmed = a > 0 or b < length
        print(f"trim  {t['n']:>2}  {t['name']:<16} " + (f"{fmt_ms(a)} > {fmt_ms(b)}  of {fmt_ms(length)}"
              if trimmed else f"none (whole file, {fmt_ms(length)})") + "  soft")
        return

    if not hard:
        project.record(f"trim {t['name']} {fmt_ms(a)}>{fmt_ms(b)}")
        project.update(t["n"], in_ms=a, out_ms=None if b >= length else b)
        if parts:
            project.part_update(parts[0]["id"], in_ms=a)
            project.part_update(parts[-1]["id"], out_ms=b)
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


def trim_part(project: Project, t: dict, part: dict, st: str | None, et: str | None) -> None:
    """A part's start and end, in timeline time; a sign moves an edge from where it is. What is
    heard stays where it was on the timeline, only less or more of the file is heard."""
    if not (st or et):
        die("trim a part with -st and -et: gout trim 3 p2 -st +200ms -et 1:45")
    label = part_label(t["parts"], part)
    start = part_start(t, part)
    end = start + part["out_ms"] - part["in_ms"]

    def moment(text: str, edge: int) -> int:
        return edge + (1 if text[0] == "+" else -1) * parse_ms(text[1:]) if text[0] in "+-" else parse_ms(text)

    new_start = moment(st, start) if st else start
    new_end = moment(et, end) if et else end
    lo, hi = part["in_ms"] + new_start - start, part["in_ms"] + new_end - start
    if lo < 0:
        die(f"{label} cannot start before its file does: at {fmt_ms(start - part['in_ms'])} at the earliest")
    if hi > t["length_ms"]:
        die(f"{label} cannot end after its file does: at {fmt_ms(start - part['in_ms'] + t['length_ms'])} at the latest")
    if hi - lo < MIN_PART_MS:
        die(f"{label} would be shorter than {MIN_PART_MS} ms")
    project.record(f"trim {t['name']} {label} {fmt_ms(new_start)}>{fmt_ms(new_end)}")
    project.part_update(part["id"], in_ms=lo, out_ms=hi)
    settle_bounds(project, t["n"])
    others = [p for p in t["parts"] if p is not part and
              part_start(t, p) < new_end and new_start < part_start(t, p) + p["out_ms"] - p["in_ms"]]
    over = f"  (overlaps {', '.join(part_label(t['parts'], p) for p in others)}: both are heard)" if others else ""
    print(f"trim  {t['n']:>2}  {t['name']:<16} {label:<8} {fmt_ms(new_start)} -> {fmt_ms(new_end)}{over}")


def settle_bounds(project: Project, n: int) -> None:
    """A track in parts reaches from its earliest part's start in the file to its latest part's end."""
    t = project.track(str(n))
    if t["parts"]:
        lo, hi = min(p["in_ms"] for p in t["parts"]), max(p["out_ms"] for p in t["parts"])
        project.update(n, in_ms=lo, out_ms=None if hi >= t["length_ms"] else hi)


def cmd_rm(project: Project, args: Args) -> None:
    delete = args.flag("-D", "--delete")
    words = args.positionals("gout rm TRACK [PART] [-D]", 1, 2)
    t = project.track(words[0])
    if len(words) == 2:
        part = pick_part(t, words[1])
        label = part_label(t["parts"], part)
        if delete:
            die("-D deletes a track's file; a part leaves the file alone, so rm TRACK PART takes no -D")
        if len(t["parts"]) == 1:
            die(f"{label} is all that is left of track {t['n']}: gout rm {t['n']} removes the track")
        project.record(f"rm {t['name']} {label}")
        project.part_delete(part["id"])
        settle_bounds(project, t["n"])
        rest = project.track(str(t["n"]))["parts"]
        if len(rest) == 1 and not part_has_settings(rest[0]) and not rest[0]["shift_ms"]:
            project.parts_clear(t["file"])  # one plain piece in place is the track, trimmed
        print(f"rm    {t['name']} {label}  (the file stays; undo brings the part back)")
        autorender(project, args)
        return
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
    usage = f"gout {column} TRACK|all [on|off]" + ("   gout mute TRACK PART [on|off]" if column == "mute" else "")
    pos = args.positionals(usage, 1, 3)
    spec, rest = pos[0], pos[1:]
    if rest and rest[0].lower() not in ("on", "off", "1", "0", "yes", "no", "true", "false"):
        if column == "solo":
            die("solo works on whole tracks; mute the parts you do not want to hear")
        if spec == "all" or len(rest) > 2:
            die(f"usage: {usage}")
        t = project.track(spec)
        part = pick_part(t, rest[0])
        label = part_label(t["parts"], part)
        new = on_off(rest[1] if len(rest) > 1 else None, part["mute"])
        project.record(f"mute {t['name']} {label} {'on' if new else 'off'}")
        project.part_update(part["id"], mute=new)
        print(f"mute  {t['n']:>2}  {t['name']:<16} {label:<8} {'on' if new else 'off'}")
        autorender(project, args)
        return
    if len(rest) > 1:
        die(f"usage: {usage}")
    state = rest[0] if rest else None
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
    words = args.positionals("gout gain TRACK [PART] DB   (e.g. gain 2 -6, gain 2 p3 -6)", 2, 3)
    t = project.track(words[0])
    part, value = (pick_part(t, words[1]) if len(words) == 3 else None), words[-1]
    try:
        gain = float(value.lower().removesuffix("db"))
    except ValueError:
        die(f"bad gain {value!r}, expected a number of dB like -6 or +3.5")
    if not -60 <= gain <= 24:
        die("gain must be between -60 and +24 dB")
    if part is not None:
        label = part_label(t["parts"], part)
        project.record(f"gain {t['name']} {label} {value}")
        project.part_update(part["id"], gain_db=gain)
        print(f"gain  {t['n']:>2}  {t['name']:<16} {label:<8} {fmt_db(gain)}")
    else:
        project.record(f"gain {t['name']} {value}")
        project.update(t["n"], gain_db=gain)
        print(f"gain  {t['n']:>2}  {t['name']:<16} {fmt_db(gain)}")
    autorender(project, args)


def cmd_pan(project: Project, args: Args) -> None:
    words = args.positionals("gout pan TRACK [PART] C | L30 | R30 | -100..100", 2, 3)
    t = project.track(words[0])
    part, value = (pick_part(t, words[1]) if len(words) == 3 else None), words[-1]
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
    if part is not None:
        label = part_label(t["parts"], part)
        project.record(f"pan {t['name']} {label} {value}")
        project.part_update(part["id"], pan=pan)
        print(f"pan   {t['n']:>2}  {t['name']:<16} {label:<8} {fmt_pan(pan)}")
    else:
        project.record(f"pan {t['name']} {value}")
        project.update(t["n"], pan=pan)
        print(f"pan   {t['n']:>2}  {t['name']:<16} {fmt_pan(pan)}")
    autorender(project, args)


# ---- parts: a track cut into pieces on its own line, each with gain, pan and mute of its own

PART_USAGE = ("gout part TRACK                         its parts\n"
              "       gout part TRACK TIME...                 cut it where you hear TIME (in the ui, here: the playhead)\n"
              "       gout part TRACK PART name [NAME]        name a part; without NAME it is p1, p2 ... again\n"
              "       gout part TRACK join [PART PART] [-f]   one piece again, or two neighbours; -f drops their own settings")
PART_RESERVED = {"on", "off", "all", "here", "join", "name", "rm", "clear", "none", "save", "master", "presets",
                 "kinds", "add", "move", "c", "center", "centre"}


def find_part(t: dict, word: str) -> dict | None:
    """One of a track's parts: p and its place from the left, or its name."""
    parts, word = t.get("parts") or [], word.lower()
    by_id = re.fullmatch(rf"{PART_WORD}#(\d+)", word)  # p#17: the part whose id is 17, as the sheet says it
    if by_id:
        return next((p for p in parts if p["id"] == int(by_id.group(1))), None)
    match = re.fullmatch(rf"{PART_WORD}(\d+)", word)
    if match:
        k = int(match.group(1))
        return parts[k - 1] if 1 <= k <= len(parts) else None
    return next((p for p in parts if p["name"] == word), None)


def pick_part(t: dict, word: str) -> dict:
    part = find_part(t, word)
    if part is None:
        if not t["parts"]:
            die(f"track {t['n']} {t['name']} is one piece, it has no part {word!r} (gout part {t['n']} TIME cuts it)")
        die(f"track {t['n']} {t['name']} has no part {word!r}; its parts: "
            + " ".join(part_label(t["parts"], p) for p in t["parts"]))
    return part


def check_part_name(t: dict, part: dict, name: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", name):
        die(f"a part name is lowercase letters, digits, - and _, up to 16, starting with a letter: {name!r}")
    if re.fullmatch(rf"{PART_WORD}\d+", name) or name in PART_RESERVED or re.fullmatch(r"[lr]\d{1,3}", name):
        die(f"{name!r} would read as something else after the track; pick another name")
    taken = next((eff.name for eff in effects().values() if name in eff.presets), None)
    if taken:
        die(f"{name!r} is a preset of {taken}; pick another name")
    if any(p["name"] == name and p["id"] != part["id"] for p in t["parts"]):
        die(f"track {t['n']} already has a part called {name}")


def part_text(parts: list[dict], part: dict) -> str:
    """p2, or p2 chorus when it has a name."""
    place = f"{PART_WORD}{parts.index(part) + 1}"
    return f"{place} {part['name']}" if part["name"] else place


def part_own(part: dict) -> str:
    bits = []
    if part["gain_db"]:
        bits.append(f"gain {fmt_db(part['gain_db'])}")
    if abs(part["pan"]) >= 0.005:
        bits.append(f"pan {fmt_pan(part['pan'])}")
    if part["mute"]:
        bits.append("muted")
    bits += [fx_text(item) for item in part.get("fx", [])]
    return ", ".join(bits)


def print_parts(t: dict) -> None:
    parts = t["parts"]
    if not parts:
        start, end = timeline(t)
        print(f"part  {t['n']:>2}  {t['name']:<16} one piece, {fmt_ms(start)} -> {fmt_ms(end)}"
              f"   (gout part {t['n']} TIME cuts it there)")
        return
    print(f"part  {t['n']:>2}  {t['name']:<16} {len(parts)} parts")
    for part in parts:
        start = part_start(t, part)
        chain = "  fx " + " | ".join(fx_text(item) for item in part["fx"]) if part.get("fx") else ""
        print((f"      {part_text(parts, part):<16} {fmt_ms(start)} -> {fmt_ms(start + part['out_ms'] - part['in_ms'])}"
               f"  {fmt_db(part['gain_db']):<7} {fmt_pan(part['pan']):<4}{'  muted' if part['mute'] else ''}{chain}").rstrip())


def cmd_part(project: Project, args: Args) -> None:
    """Cut a track into parts, name them, join them again."""
    force = args.flag("--force", "-f")
    words = args.positionals(PART_USAGE, 1)
    t = project.track(words[0])
    rest = words[1:]
    if not rest:
        print_parts(t)
        return
    if rest[0].lower() == "join":
        join_parts(project, t, rest[1:], force)
    elif len(rest) >= 2 and rest[1].lower() == "name":
        part = pick_part(t, rest[0])
        if len(rest) > 3:
            die(f"a part name is one word\nusage: {PART_USAGE}")
        new = rest[2].lower() if len(rest) == 3 else None
        if new is not None:
            check_part_name(t, part, new)
        project.record(f"part {t['name']} {part_label(t['parts'], part)} name {new or ''}".rstrip())
        project.part_update(part["id"], name=new)
    elif rest[0].lower() == "here":
        die("here is the playhead, so it works at the ui's prompt; from a shell give the time: "
            f"gout part {t['n']} 1:30")
    elif find_part(t, rest[0]) is not None:
        die(f"what should {rest[0]} do? A part takes name, or join two of them: gout part {t['n']} join {' '.join(rest[:2])}"
            f"\nusage: {PART_USAGE}")
    else:
        cut_track(project, t, [parse_ms(word) for word in rest])
    print_parts(project.track(str(t["n"])))
    autorender(project, args)


def cut_track(project: Project, t: dict, times: list[int]) -> None:
    """Cut at timeline times. Worked out in full before anything is written, so a bad time changes nothing."""
    a, b = audible(t)
    pieces = [dict(p) for p in t["parts"]] or [
        {"id": None, "name": None, "in_ms": a, "out_ms": b, "shift_ms": 0, "gain_db": 0.0, "pan": 0.0, "mute": False}]
    for at in sorted(set(times)):
        piece = next((p for p in pieces if part_start(t, p) < at < part_start(t, p) + p["out_ms"] - p["in_ms"]), None)
        if piece is None:
            start, end = timeline(t)
            die(f"track {t['n']} {t['name']} has nothing to cut at {fmt_ms(at)}: it plays {fmt_ms(start)} -> {fmt_ms(end)}"
                + (" and is already cut there" if start < at < end else ""))
        cut = at - t["offset_ms"] - piece["shift_ms"]
        if cut - piece["in_ms"] < MIN_PART_MS or piece["out_ms"] - cut < MIN_PART_MS:
            die(f"a cut at {fmt_ms(at)} would leave a part shorter than {MIN_PART_MS} ms")
        after = {**piece, "id": None, "name": None, "in_ms": cut}
        piece["out_ms"] = cut
        pieces.insert(pieces.index(piece) + 1, after)
    project.record(f"part {t['name']} " + " ".join(fmt_ms(at) for at in sorted(set(times))))
    for piece in pieces:
        if piece["id"] is None:
            pid = project.part_insert(t["file"], name=piece["name"], in_ms=piece["in_ms"], out_ms=piece["out_ms"],
                                      shift_ms=piece["shift_ms"], gain_db=piece["gain_db"], pan=piece["pan"],
                                      mute=int(piece["mute"]))
            for item in piece.get("fx", []):  # both halves of a cut part keep its effects
                project.fx_insert(part_owner(t["file"], pid), item["kind"], item["params"], on=item["on"])
        else:
            project.part_update(piece["id"], out_ms=piece["out_ms"])


def join_parts(project: Project, t: dict, words: list[str], force: bool) -> None:
    parts = t["parts"]
    if not parts:
        die(f"track {t['n']} {t['name']} is one piece already")
    if words:
        if len(words) != 2:
            die(f"join takes two neighbouring parts, or none for all of them\nusage: {PART_USAGE}")
        i, j = sorted(parts.index(pick_part(t, word)) for word in words)
        if j != i + 1:
            die(f"{part_label(parts, parts[i])} and {part_label(parts, parts[j])} are not next to each other")
        group = parts[i:j + 1]
    else:
        group = parts
    for left, right in zip(group, group[1:]):
        if not meets(left, right):
            die(f"{part_label(parts, left)} and {part_label(parts, right)} do not meet, so they cannot be joined")
    alike = len({part_settings(p) for p in group}) == 1
    if not alike and not force:
        own = [f"{part_label(parts, p)} ({part_own(p)})" for p in group if part_has_settings(p)]
        die(f"{', '.join(own)} {'has settings of its own' if len(own) == 1 else 'have settings of their own'} that joining would drop; "
            f"gout part {t['n']} join{''.join(' ' + w for w in words)} -f joins anyway (undo brings them back)")
    project.record(f"part {t['name']} join" + "".join(" " + part_label(parts, p) for p in (group if words else [])))
    first = group[0]
    kept = {} if alike else {"gain_db": 0.0, "pan": 0.0, "mute": 0}
    project.part_update(first["id"], out_ms=group[-1]["out_ms"], **kept)
    if not alike:
        project.fx_clear(part_owner(t["file"], first["id"]))
    for part in group[1:]:
        project.part_delete(part["id"])
    if not alike:
        print(f"join  dropped " + "; ".join(f"{part_label(parts, p)} {part_own(p)}" for p in group if part_has_settings(p)))
    left = project.track(str(t["n"]))["parts"]
    if len(left) == 1 and not part_has_settings(left[0]):
        project.parts_clear(t["file"])  # one plain piece is the track itself


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


def owner_and_words(project: Project, spec: str, words: list[str]) -> tuple[dict, list[str]]:
    """The chain an effect command works on: the track's, or a part's when the first word after
    the track names one of its parts (eq 3 p2 hp80)."""
    t = chain_owner(project, spec)
    if words and t["owner"] != MASTER_OWNER and t["parts"]:
        part = find_part(t, words[0])
        if part is not None:
            return part_as_owner(t, part), words[1:]
        if re.fullmatch(rf"{PART_WORD}\d+", words[0].lower()):
            pick_part(t, words[0])  # p9 on a track in three parts: say so, not "bad band"
    return t, words


def owner_label(t: dict) -> str:
    return "master" if t["owner"] == MASTER_OWNER else t.get("label", t["name"])


def owner_spec(t: dict) -> str:
    return "master" if t["owner"] == MASTER_OWNER else t.get("spec", str(t["n"]))


def refresh(project: Project, t: dict) -> dict:
    if "part" in t:
        track = project.track(str(t["n"]))
        part = next(p for p in track["parts"] if p["id"] == t["part"]["id"])
        return part_as_owner(track, part)
    return chain_owner(project, owner_spec(t))


def effect_usage(eff: Effect) -> str:
    text = (f"gout {eff.name} TRACK|master [PART] [SETTINGS... | PRESET | on | off | clear]\n"
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
        t, words = owner_and_words(project, pos[0], pos[1:])
        item = first_of(t, eff.name)
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
        pos = args.positionals(f"gout {name} TRACK|master [PART] {usage}", 2)
        t, words = owner_and_words(project, pos[0], pos[1:])
        item = first_of(t, eff.name)
        try:
            line = apply(item["params"] if item else "", words)
        except ValueError as exc:
            die(str(exc))
        line = checked_line(project, t, eff, line)
        project.record(f"{name} {owner_label(t)} {' '.join(words)}")
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


FX_USAGE = """gout fx TRACK|master [PART]                   the effects in order, numbered (a part's after its name)
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
    t, words = owner_and_words(project, pos[0], pos[1:])
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


DUP_USAGE = "gout duplicate TRACK FROM TO [-a AT] [-n NAME]   (dup) a new track with what TRACK plays from FROM to TO"


def cmd_duplicate(project: Project, args: Args) -> None:
    """A stretch of a track's audio, as heard on the timeline, into a new file and a new track: at
    the same time unless -a says where. The audio only; effects, gain and pan stay with the old track."""
    at = args.value("--at", "-a")
    name = args.value("--name", "-n")
    words = args.positionals(DUP_USAGE, 3, 3)
    t = project.track(words[0])
    low, high = parse_ms(words[1]), parse_ms(words[2])
    if high - low < MIN_PART_MS:
        die(f"duplicate from {fmt_ms(low)} to {fmt_ms(high)}: {MIN_PART_MS} ms at least, and TO after FROM")
    a, b = audible(t)
    pieces = t["parts"] or [{"in_ms": a, "out_ms": b, "shift_ms": 0}]
    heard = [p for p in pieces if part_start(t, p) < high and low < part_start(t, p) + p["out_ms"] - p["in_ms"]]
    if not heard:
        start, end = timeline(t)
        die(f"track {t['n']} {t['name']} plays nothing from {fmt_ms(low)} to {fmt_ms(high)} (it plays {fmt_ms(start)} -> {fmt_ms(end)})")
    if len({p["shift_ms"] for p in heard}) > 1:
        die(f"that stretch covers parts of track {t['n']} that were moved apart; duplicate them one by one")
    zero = t["offset_ms"] + heard[0]["shift_ms"]  # where the file starts on the timeline
    file_from = max(min(p["in_ms"] for p in heard), low - zero)
    file_to = min(max(p["out_ms"] for p in heard), high - zero)
    track_name = project.unique_name(name or f"{t['name']}-copy")
    dst = project.tracks_dir / f"{track_name}.wav"
    run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{file_from / 1000:.3f}",
               "-t", f"{(file_to - file_from) / 1000:.3f}", "-i", str(project.tracks_dir / t["file"]),
               "-map", "0:a:0", "-c:a", "pcm_f32le", str(dst)], args.verbose)
    project.record(f"duplicate {t['name']} {fmt_ms(low)}>{fmt_ms(high)}")
    ingest(project, dst, track_name, parse_ms(at) if at else zero + file_from, args.verbose, verb="dup")
    project.created([dst.name])
    autorender(project, args)


LOOP_USAGE = "gout loop FROM TO | on | off   (loop alone says what it is; lo for short)"


def loop_range(project: Project, every: bool = False) -> tuple[int, int] | None:
    """The loop (from, to) in project ms when it is on (or set at all, with every)."""
    text = project.get("ui_loop") or ""
    if not every and project.get("ui_loop_on") != "on":
        return None
    try:
        low, high = (int(v) for v in text.split("-", 1))
    except ValueError:
        return None
    return (low, high) if high > low else None


def cmd_loop(project: Project, args: Args) -> None:
    """Play a stretch of the song over and over: in the ui, gout play, and the window (l)."""
    words = args.positionals(LOOP_USAGE, 0, 2)
    low_words = [w.lower() for w in words]
    if low_words == ["off"]:
        project.set("ui_loop_on", "off")
    elif low_words == ["on"]:
        if loop_range(project, every=True) is None:
            die(f"no loop to switch on yet: gout loop 1:30 1:45\nusage: {LOOP_USAGE}")
        project.set("ui_loop_on", "on")
    elif len(words) == 2:
        low, high = parse_ms(words[0]), parse_ms(words[1])
        if high - low < 50:
            die(f"a loop ends after it starts, 50 ms at least: {fmt_ms(low)} -> {fmt_ms(high)}")
        project.set("ui_loop", f"{low}-{high}")
        project.set("ui_loop_on", "on")
    elif words:
        die(f"usage: {LOOP_USAGE}")
    shown = loop_range(project, every=True)
    if shown is None:
        print("loop  none (gout loop 1:30 1:45 sets one)")
    else:
        on = project.get("ui_loop_on") == "on"
        print(f"loop  {fmt_ms(shown[0])} -> {fmt_ms(shown[1])}  {fmt_ms(shown[1] - shown[0])} long, "
              + ("on: playing goes round it" if on else "off (loop on brings it back)"))


def player_for(project: Project, from_ms: int, render: bool = False, loop: bool = True, to_ms: int | None = None) -> Player:
    """A player for the project from from_ms: master.wav when it matches the project (or after
    rendering it, with render), otherwise the project streamed live. Not started yet. With the loop
    on (and loop), it plays to the loop's end and then round the loop; from outside it, from its start."""
    if render and not project.master_is_current():
        print(f"play  rendering {MASTER_WAV} first")
        mix(project)
    stretch = loop_range(project) if loop and to_ms is None else None  # a stretch played to its end does not loop
    if stretch is not None and not stretch[0] <= from_ms < stretch[1]:
        from_ms = stretch[0]
    if project.master_is_current():
        length = probe(project.master)["duration"]
        head = head_seconds(project)
        if from_ms / 1000 + head >= length:
            die(f"{fmt_ms(from_ms)} is past the end of {MASTER_WAV} ({fmt_ms((length - head) * 1000)})")
        if stretch is not None:
            high = min(length, stretch[1] / 1000 + head)
            return Player(project.master, from_ms / 1000 + head, length, loop=(stretch[0] / 1000 + head, high))
        return Player(project.master, from_ms / 1000 + head, length,
                      end_s=None if to_ms is None else min(length, to_ms / 1000 + head))
    warnings: set[str] = set()
    if stretch is not None:
        source = live_source(project, stretch[0], warnings)
        for warning in sorted(warnings):
            print(f"      {warning}")
        if source is None:
            die(f"nothing to play in the loop {fmt_ms(stretch[0])} -> {fmt_ms(stretch[1])}")
        args, length_ms = source
        high = stretch[0] + min(length_ms, stretch[1] - stretch[0])
        return Player(None, from_ms / 1000, high / 1000, stream=args, loop=(stretch[0] / 1000, high / 1000),
                      stream_start=stretch[0] / 1000)
    source = live_source(project, from_ms, warnings)
    for warning in sorted(warnings):
        print(f"      {warning}")
    if source is None:
        die("nothing to play: no track is audible there" + (f" from {fmt_ms(from_ms)}" if from_ms else ""))
    args, length_ms = source
    if length_ms <= 0:
        die(f"{fmt_ms(from_ms)} is past the end of the project")
    end = (from_ms + length_ms) / 1000
    return Player(None, from_ms / 1000, end, stream=args, end_s=None if to_ms is None else min(end, to_ms / 1000))


def head_seconds(project: Project) -> float:
    """master.wav starts with the head padding, so project time 0 is this far into the file."""
    return int(setting(project, "head")) / 1000


def progress_bar(position: float, length: float, width: int = 30) -> str:
    done = round(width * position / length) if length else 0
    return "━" * done + "─" * (width - done)


def cmd_play(project: Project, args: Args) -> None:
    render = args.flag("--render", "-r")
    pos = args.positionals("gout play [FROM [TO]] [-r]   e.g. gout play 1:30, gout play 1:30 1:45  (-r renders first; ctrl-c stops)", 0, 2)
    start = parse_ms(pos[0]) / 1000 if pos else 0.0
    to_ms = parse_ms(pos[1]) if len(pos) > 1 else None
    if to_ms is not None and to_ms <= start * 1000:
        die(f"play FROM TO: {fmt_ms(to_ms)} is not after {fmt_ms(start * 1000)}")
    player = player_for(project, round(start * 1000), render, to_ms=to_ms).start()
    head = 0.0 if player.live else head_seconds(project)
    length = player.length_s
    what = "live (master.wav is out of date; mix or play -r renders it)" if player.live else MASTER_WAV
    stretch = loop_range(project)
    if stretch is not None and to_ms is None:
        start = player.start_s - head
        what += f", round the loop {fmt_ms(stretch[0])} -> {fmt_ms(stretch[1])} (gout loop off ends it)"
    if to_ms is not None:
        what += f" to {fmt_ms(to_ms)}"
        length = player.end_s
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


RECORD_USAGE = ("gout record [FROM] [-t LENGTH] [-n NAME] [-i INPUT] [-c N] [-s] [-d] [-M]   (ctrl-c stops)\n"
                "       gout record check [FROM] [-t LENGTH] [-i INPUT] [-c N] [-s] [-d] [-M]   levels only, nothing kept")


def play_along(project: Project, from_ms: int) -> list[str] | None:
    """ffmpeg arguments for what plays from from_ms (master.wav when current, else live), or None
    when nothing is audible from there."""
    try:
        return player_for(project, from_ms, loop=False).source_args()  # a take goes straight on
    except GoutError as exc:
        print(f"rec   {exc}: recording without playing")
        return None


class Take:
    """One take, or a level check, running while its caller does something else: the shell waits in a
    loop and draws a meter, the ui goes on drawing its screen. start_take makes one."""

    def __init__(self, project: Project, recorder, monitor: Monitor, track_name: str, at_ms: int, checking: bool,
                 args: Args):
        self.project, self.recorder, self.monitor = project, recorder, monitor
        self.track_name, self.at_ms, self.checking, self.args = track_name, at_ms, checking, args
        self.loudest, self.clips = 0.0, 0
        self.peaks: list[float] = []  # the peak of every poll, for a waveform that grows
        self.done = False

    def running(self) -> bool:
        return not self.done and self.recorder.running()

    def poll(self) -> float:
        """The loudest sample since the last poll (call it about every 0.1 s)."""
        peak = self.recorder.take_peak()
        self.loudest = max(self.loudest, peak)
        self.clips += peak >= CLIP
        self.peaks.append(peak)
        return peak

    def position_ms(self) -> int:
        return self.at_ms + round(self.recorder.seconds() * 1000)

    def status(self, peak: float) -> str:
        what = "CHECK" if self.checking else "REC"
        return (f"● {what} {fmt_ms(self.position_ms() - self.at_ms)}  {meter(peak)}  loudest "
                f"{level_db(self.loudest):5.1f} dB  clips {self.clips}")

    def finish(self) -> None:
        """Stop the input and keep the take as a track, or for a check say what the level was.
        Prints what happened; GoutError when nothing came in."""
        if self.done:
            return
        self.done = True
        recorder = self.recorder
        ended = recorder.ended_by_itself()
        recorder.stop()
        self.monitor.stop()
        if not self.checking:
            keep_take(self.project, recorder, self.track_name, self.at_ms, ended, self.args)
            return
        loudest = max(self.loudest, recorder.top)
        frames, complaint = recorder.frames, recorder.complaint()
        recorder.path.unlink(missing_ok=True)
        if not frames:
            die("nothing came in from the input" + (f": {complaint}" if complaint else ""))
        print(f"check {level_advice(loudest, self.clips + (1 if recorder.clipped and not self.clips else 0))}")


def start_take(project: Project, args: Args, from_ms: int = 0) -> Take:
    """Read the record options, start the input (with the project playing along and the input in the
    headphones when it can) and hand back the running take. Prints what it set up."""
    name = args.value("--name", "-n")
    spec = args.value("--in", "-i")
    channel = args.value("--channel", "-c", default="1")
    length = args.value("--time", "-t")
    stereo = args.flag("--stereo", "-s")
    dry = args.flag("--dry", "-d")
    quiet = args.flag("--no-monitor", "-M")
    pos = args.positionals(RECORD_USAGE, 0, 2)
    checking = bool(pos) and pos[0].lower() == "check"
    if checking:
        pos = pos[1:]
    if len(pos) > 1:
        die(f"usage: {RECORD_USAGE}")
    if not channel.isdigit() or int(channel) < 1:
        die(f"-c takes the input channel to record, 1 or more, not {channel!r}")
    at_ms = parse_ms(pos[0]) if pos else from_ms
    max_frames = None
    if length:
        max_frames = round(parse_ms(length) * project.rate / 1000)
        if max_frames <= 0:
            die(f"-t {length}: record for longer than that")
    backend = choose_backend()
    device, note = current_input(list_inputs(backend), spec)
    if note:
        print(f"rec   {note}")
    if input_is_muted(backend, device):
        print(f"rec   {device.label} is muted in the system, so this records silence: unmute it in Settings > Sound"
              " > Input, or pactl set-source-mute " + device.name + " 0")
    first, count = int(channel), 2 if stereo else 1
    play = None
    if not dry:
        if duplex_available(backend):
            play = play_along(project, at_ms)
        else:
            print(f"rec   the project does not play while recording without PortAudio ({install_hint()})")
    track_name = project.unique_name(name or "rec")
    path = project.tracks_dir / f"{track_name}.wav"
    if checking:  # the same take, into the cache, gone at the end
        path = project.root / ".gout" / "check.part.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
    cmd, why = (None, "") if quiet else monitor_plan(backend, device, output_for(backend))
    monitor = Monitor(cmd)
    if play is None:
        recorder = Recorder(path, project.rate, device, backend, first, count, max_frames).start()
    else:
        recorder = Engine(path, project.rate, device, backend, first, count, max_frames, play,
                          output_for(backend)).start()
    monitor.start()
    which = f"channels {first}+{first + 1}" if stereo else f"channel {first}"
    how = "playing from there" if play is not None else "not playing"
    where = "checking levels, nothing is kept" if checking else f"-> {TRACK_DIR}/{path.name}"
    print(f"rec   {device.label}, {which} {where} at {fmt_ms(at_ms)}, {how}"
          f"  ({'stops after ' + fmt_ms(parse_ms(length)) if length else STOP_HINT})", flush=True)
    if why:
        print(f"rec   {why}", flush=True)
    return Take(project, recorder, monitor, track_name, at_ms, checking, args)


STOP_HINT = "ctrl-c stops"


def cmd_record(project: Project, args: Args) -> None:
    """Record from an input into a new track at FROM while the project plays from FROM, until
    ctrl-c or LENGTH. -d, or no PortAudio, records without playing. record check: levels only."""
    take = start_take(project, args)
    live = sys.stdout.isatty()
    try:
        while take.running():
            peak = take.poll()
            if live:
                print(f"\r      {take.status(peak)}  ", end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    if live:
        print()
    take.finish()


def level_advice(loudest: float, clips: int) -> str:
    """What a level check found, and what to do about it."""
    db = level_db(loudest)
    if loudest <= 0:
        return "nothing came in: is the input muted, or the synth turned down? (gout inputs shows which input)"
    if clips:
        return (f"it clipped {clips} time{'' if clips == 1 else 's'}: turn the input (Settings > Sound > Input) or "
                "the instrument down, at least 3 dB, and check again")
    if db > -3:
        return f"loudest {db:.1f} dB: close to the top, 3 to 6 dB down leaves room for a louder moment"
    if db < -18:
        return f"loudest {db:.1f} dB: quiet, about {round(-12 - db)} dB up would be better"
    return f"loudest {db:.1f} dB, no clips: a good level"


def keep_take(project: Project, recorder, track_name: str, at_ms: int, ended: bool, args: Args) -> None:
    """Register a finished take as a track (undo deletes its file), or remove an empty one. A take
    recorded along with the project starts its latency before FROM, soft-trimmed off, so what
    was played at FROM lines up with FROM and trim -c keeps it lined up."""
    complaint = recorder.complaint()
    if recorder.frames == 0:
        recorder.path.unlink(missing_ok=True)
        die("nothing was recorded" + (f": {recorder.backend} said {complaint}" if complaint else ""))
    if ended and not recorder.backend.startswith("file:"):
        print("rec   the input stopped by itself" + (f": {complaint}" if complaint else ""))
    project.record(f"record {track_name}")
    track, _ = ingest(project, recorder.path, track_name, at_ms, args.verbose, verb="rec")
    project.created([recorder.path.name])
    shift = recorder.correction_ms
    if shift:
        project.update(track["n"], offset_ms=at_ms - shift, in_ms=max(0, shift))
    for line in recorder.note.splitlines():
        print(f"      {line}")
    db = level_db(recorder.top)
    level = "silent" if db == float("-inf") else f"{db:.1f} dBFS"
    advice = "  it clipped: turn the input down and record again" if recorder.clipped else (
        "  very quiet: is it the right input? (gout inputs)" + (
            " On macOS the terminal needs the microphone: System Settings, Privacy & Security, Microphone"
            if recorder.backend == "avfoundation" else "") if recorder.top < 0.001 else "")
    print(f"      peak {level}{advice}")
    autorender(project, args)


def cmd_calibrate(root_hint: Path | None, args: Args) -> None:
    """Play clicks and record them: the latency outside the buffers for this input and output,
    which every take recorded along with the project is lined up with from then on."""
    import statistics
    import tempfile
    spec = args.value("--in", "-i")
    channel = args.value("--channel", "-c", default="1")
    args.positionals("gout record calibrate [-i INPUT] [-c N]", 0, 0)
    if not channel.isdigit() or int(channel) < 1:
        die(f"-c takes the input channel to record, 1 or more, not {channel!r}")
    backend = choose_backend()
    if not duplex_available(backend):
        die(f"calibrating plays and records at once, which needs PortAudio ({install_hint()})")
    device, note = current_input(list_inputs(backend), spec)
    if note:
        print(f"calib {note}")
    output = output_for(backend)
    folder = Path(tempfile.mkdtemp(prefix="gout-calibrate-"))
    try:
        engine = Engine(folder / "clicks.wav", DEFAULT_RATE, device, backend, int(channel), 1,
                        output_name=output, clicks=CLICK_TIMES)
        print(f"calib {len(CLICK_TIMES)} clicks through {output}, recorded from {device.label}, channel {channel}:"
              " the microphone near the speaker, or a cable from the output to the input", flush=True)
        engine.start()
        try:
            while engine.running():
                time.sleep(0.1)
        except KeyboardInterrupt:
            engine.stop()
            die("calibration stopped")
        engine.stop()
        if not engine.result:
            die(f"the clicks were not recorded: {engine.complaint() or 'no answer from the engine'}")
        rate = engine.rate
        take = array_of_take(folder / "clicks.wav")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    offsets, above = click_offsets(take, rate, CLICK_TIMES)
    if len(offsets) < 8:
        heard = ("nothing came back" if not take or max(max(take), -min(take)) == 0 else
                 f"heard {len(offsets)} of {len(CLICK_TIMES)} clicks clearly above the noise")
        die(f"{heard}: turn the speaker up, put the microphone closer, or use a cable (gout inputs: the right input?)")
    spread = max(offsets) - min(offsets)
    if spread > 0.002 * rate:
        die(f"the clicks came back up to {spread * 1000 / rate:.1f} ms apart: too much echo or noise to trust;"
            " try closer, somewhere quieter, or with a cable")
    key = engine.key()
    before = calibration(key)
    constant = statistics.median_low(offsets) - int(engine.result.get("lag", 0))
    save_calibration(key, constant, rate, spread)
    was = f"; it was {before * 1000 / rate:.1f} ms" if before is not None else ""
    print(f"calib {constant * 1000 / rate:.1f} ms outside the buffers ({len(offsets)} of {len(CLICK_TIMES)} clicks,"
          f" within {spread * 1000 / rate:.1f} ms{was})")
    print(f"      takes recorded from {device.label} while playing through {output} are lined up with it from now on")
    if engine.clipped:
        print("      the clicks clipped the input: turn it down a little and calibrate again")


def array_of_take(path: Path):
    """The samples of a mono take TakeWriter wrote."""
    from array import array
    from .media import FLOAT_WAV_HEADER
    data = array("f")
    raw = path.read_bytes()[FLOAT_WAV_HEADER:]
    data.frombytes(raw[:len(raw) // 4 * 4])
    if sys.byteorder == "big":
        data.byteswap()
    return data


def cmd_inputs(root_hint: Path | None, args: Args) -> None:
    """The inputs gout can record from here, and which one it uses; N picks one for this computer."""
    (spec,) = args.positionals("gout inputs [N | NAME | default]   (N or NAME: record from that input on this"
                               " computer; default: the system's own again)", 0, 1) or [None]
    backend = choose_backend()
    inputs = list_inputs(backend)
    if spec == "default":
        save_settings(input=None)
        print("inputs gout record uses the system's default input again")
    elif spec is not None:
        chosen = find_input(inputs, spec)
        save_settings(input=chosen.name)
        print(f"inputs gout record uses {chosen.label} on this computer")
    if not inputs:
        print(f"inputs {backend}: no inputs found")
        return
    current, note = current_input(inputs)
    picked = "picked for this computer" if load_settings().get("input") == current.name else "the system default"
    from .portaudio import available, version
    along = (f"PortAudio {version().removeprefix('PortAudio ').split(',')[0]} plays the project while recording"
             if available() else f"no PortAudio, so the project does not play while recording ({install_hint()})")
    print(f"inputs {backend}   * gout record uses this ({picked}); {along}")
    if note:
        print(f"       {note}")
    for k, item in enumerate(inputs, 1):
        mark = "*" if item.name == current.name else " "
        channels = f"{item.channels} ch" if item.channels else ""
        print(f"  {mark} {k:>2}  {item.label:<44} {channels:>5}{'  default' if item.default else ''}")
        if args.verbose:
            print(f"          {item.name}")
    for key, value in sorted((load_settings().get("calibration") or {}).items()):
        if key.startswith(f"{current.name} -> ") and isinstance(value, dict):
            output, _, where = key.split(" -> ", 1)[1].rpartition(" @ ")
            print(f"       calibrated with {output} ({where.split(' / ')[0]}): {value.get('ms')} ms ({value.get('date')})")
    print("       gout inputs N picks one for this computer; gout record -i N uses one once; -v shows names;"
          " gout record calibrate lines takes up")


VIDEO_USAGE = ("gout video IMAGE [-T] [-o FILE]                          a still image with the song\n"
               "       gout video SCREEN [CHOICE... | all] [-e 10s] [-c IMAGE] [-T] [-o FILE]  a screen moving with the song,\n"
               "       e.g. gout video fractal 3 1 0 -c cover.jpg  (the cover first, a new fractal every 10 s on a drum hit;\n"
               "       gout video zoom 3 dives into preset 3 for the whole song;\n"
               "       the title and artist from set title / set artist at the start unless -T)")


def cmd_video(project: Project, args: Args) -> None:
    """An mp4 of the song for YouTube and the like: master.wav with a picture, or with a screen
    such as the fractal drawn frame by frame."""
    import math
    from .analysis import FRAME_MS, project_features
    from .screens import ScreenContext, screen_named
    from .video import (CELL_H, CELL_W, compose, COVER_SECONDS, cut_points, class_colours, Atlas, Encoder, EVERY_MS, FPS,
                        grid, picture, Progress, schedule, ScreenFrames, Title, TITLE_UNTIL, workers)
    out = args.value("--out", "-o")
    every = args.value("--every", "-e")
    cover = args.value("--cover", "-c")
    no_title = args.flag("--no-title", "-T")
    words = args.positionals(VIDEO_USAGE, 1)
    target = Path(out).expanduser() if out else project.root / "master.mp4"
    image = Path(words[0]).expanduser()
    screen = None if image.is_file() else screen_named(words[0])
    if screen is None and not image.is_file():
        die(f"no such image or screen: {words[0]}\nusage: {VIDEO_USAGE}")
    if screen is None and (len(words) > 1 or cover):
        die(f"an image takes nothing after it\nusage: {VIDEO_USAGE}")
    if cover and not Path(cover).expanduser().is_file():
        die(f"no such image: {cover}")
    every_ms = parse_ms(every) if every else EVERY_MS
    if every_ms < 1000:
        die("-e: a choice lasts a second at least")
    choices = words[1:]
    if screen is not None:
        if [w.lower() for w in choices] == ["all"]:
            choices = screen.choices()
            if not choices:
                die(f"the {screen.name} screen has no list to go through{older_example_hint(screen)}")
        ctx = ScreenContext(project)
        for word in dict.fromkeys(choices):  # every choice checked before minutes of work
            try:
                screen.pick(ctx, word)
            except ValueError as exc:
                die(f"{screen.name} {word}: {exc}")
        if not choices:  # the screen as it opens in the ui: the fractal's last preset
            try:
                lines, ok = screen.command(ctx, [])
            except ValueError as exc:
                die(f"{screen.name}: {exc}")
            if not ok:
                die("; ".join(lines) or f"the {screen.name} screen wants a choice: gout video {screen.name} CHOICE")
        hint = older_example_hint(screen)
        if hint:
            print(f"video {hint.strip(' ()')}")
    if not project.master_is_current():
        print(f"video rendering {MASTER_WAV} first")
        mix(project)
    if not project.master.exists():
        die("nothing audible to make a video of")
    seconds = probe(project.master)["duration"]
    cols, rows = grid()
    width, height = cols * CELL_W, rows * CELL_H
    frames = math.ceil(seconds * FPS)
    title_text, artist = setting(project, "title"), setting(project, "artist")
    title = None if no_title or not (title_text or artist) else Title(title_text, artist, cols, rows,
                                                                      until=min(TITLE_UNTIL, seconds / 2))
    if not no_title and title is None:
        print("video no title: gout set title TEXT (and set artist TEXT) puts one at the start")
    cover_until = -1.0
    if screen is None:
        what, source = image.name, None
        still = picture(image, width, height)
    else:
        features = project_features(project)  # works out and caches the analysis before the workers read it
        head_ms = int(setting(project, "head"))
        cuts = cut_points(features.values["onset"], every_ms, features.length_ms, FRAME_MS) if len(choices) > 1 else []
        count = min(workers(), frames)
        what = f"the {screen.name} screen" + (f": {' '.join(choices)}" if choices else "")
        if len(choices) > 1:
            what += f", {len(cuts)} changes about every {fmt_ms(every_ms)}"
        tasks = schedule(frames, FPS, head_ms, cuts, choices)
        if cover:
            still = picture(Path(cover).expanduser(), width, height)
            wanted = min(COVER_SECONDS, seconds / 3) * 1000 - head_ms  # a short song still gets its screen
            cover_until = (cut_points(features.values["onset"], round(wanted), round(wanted) + 2001, FRAME_MS)[:1]
                           or [wanted])[0] / 1000 + head_ms / 1000
            what += f", after {Path(cover).name} for {cover_until:.1f} s"
            tasks = [task for task in tasks if task[0] / FPS >= cover_until]
            count = max(1, min(count, len(tasks)))
        source = ScreenFrames(project, screen.name, tasks, cols, rows, round(seconds * 1000) - head_ms, count,
                              first=next((t[0] for t in tasks), frames), cuts=cuts)
    print(f"video {target.name}  {width}x{height} {FPS} fps  {fmt_ms(seconds * 1000)}  {what}"
          + (f"  ({count} processes drawing)" if source else "") + "  (ctrl-c stops)", flush=True)
    atlas, colours = ((Atlas(extra=title_text + artist), class_colours(project.root)) if source is not None or title is not None
                      else (None, None))
    encoder = Encoder(target, project.master, width, height, FPS)
    progress = Progress(frames)
    try:
        for done in range(frames):
            at = done / FPS
            on_title = title is not None and at < title.until
            if source is None or at < cover_until:
                frame = compose(title.over([], at), atlas, colours, cols, rows, background=still) if on_title else still
            else:
                rows_now = source.rows(done)
                frame = compose(title.over(rows_now, at) if on_title else rows_now, atlas, colours, cols, rows)
            encoder.write(frame)
            progress.update(done + 1)
    except (KeyboardInterrupt, GoutError) as exc:
        if source is not None:
            source.stop()
        encoder.abort()
        progress.close()
        die(str(exc) if isinstance(exc, GoutError) else "video stopped; nothing written")
    progress.close()
    encoder.finish()
    print(f"      {target}  {fmt_size(target.stat().st_size)}")


def older_example_hint(screen) -> str:
    """When a screen's addon file is a copy of an example that has changed since: how to update it."""
    source = Path(screen.source)
    example = resource_dir() / "examples" / "addons" / source.name
    if source.is_file() and example.is_file() and source.read_bytes() != example.read_bytes():
        return (f" ({source.name} differs from the one that comes with gout; gout addons examples --update"
                f" brings the new one and keeps yours as {source.name}.bak)")
    return ""


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
        if sounds(project, t):
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
            if isinstance(item.get("parts"), list):
                apply_parts(project, t, item["parts"], warnings)
            if isinstance(item.get("fx"), list):
                apply_chain(project, t["file"], t["name"], item["fx"], warnings)
            elif any(item.get(kind) is not None for kind in ("eq", "comp", "delay", "reverb")):
                apply_chain(project, t["file"], t["name"], legacy_chain(item), warnings)
            order.append(t["file"])
            n_tracks += 1
        if order:
            project.reorder(order)
    return n_settings, n_tracks, warnings


def apply_parts(project: Project, t: dict, items: list, warnings: list[str]) -> None:
    """A track's parts from a document, each checked against the file."""
    project.parts_clear(t["file"])
    names: set[str] = set()
    for item in items:
        try:
            lo, hi = int(item["in_ms"]), int(item["out_ms"])
            name = item.get("name") or None
            fields = {"shift_ms": int(item.get("shift_ms") or 0), "gain_db": max(-60.0, min(24.0, float(item.get("gain_db") or 0))),
                      "pan": max(-1.0, min(1.0, float(item.get("pan") or 0))), "mute": 1 if item.get("mute") else 0}
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{t['name']}: a part with bad values ({exc}), skipped")
            continue
        if not 0 <= lo < hi <= t["length_ms"]:
            warnings.append(f"{t['name']}: a part {fmt_ms(lo)} > {fmt_ms(hi)} is outside the file, skipped")
            continue
        if name is not None and (not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", str(name)) or name in names):
            warnings.append(f"{t['name']}: part name {name!r} not usable, left unnamed")
            name = None
        names.add(name)
        pid = project.part_insert(t["file"], name=name, in_ms=lo, out_ms=hi, **fields)
        if isinstance(item.get("fx"), list):
            apply_chain(project, part_owner(t["file"], pid), f"{t['name']} {name or 'part'}", item["fx"], warnings)


def read_document(path: Path) -> dict:
    if not path.is_file():
        die(f"no such file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    for label, cells, _, _, _ in render_timeline(project, width, loop=loop_range(project)):
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


def copy_examples(folder: Path, update: bool = False) -> None:
    """The example addons that come with gout into the addon folder; files already there stay, or
    with update are replaced when they differ, the old one kept as NAME.py.bak."""
    examples = resource_dir() / "examples" / "addons"
    found = sorted(examples.glob("*.py")) if examples.is_dir() else []
    if not found:
        die(f"this gout has no example addons with it (looked in {examples})")
    folder.mkdir(parents=True, exist_ok=True)
    for src in found:
        dst = folder / src.name
        if dst.exists() and update and dst.read_bytes() != src.read_bytes():
            shutil.copy2(dst, dst.with_name(dst.name + ".bak"))
            shutil.copy2(src, dst)
            print(f"       {src.name:<24} updated; the old one is {dst.name}.bak")
        elif dst.exists():
            print(f"       {src.name:<24} already there, kept as it is" + ("" if update else
                  " (--update replaces it when it differs)"))
        else:
            shutil.copy2(src, dst)
            print(f"       {src.name:<24} copied")
    print("       docs/addons.md in gout's source tells how to write your own")


def cmd_addons(root_hint: Path | None, args: Args) -> None:
    """Where addons are read from, what loaded, and what the project here uses but lacks."""
    from .addons import addon_dir, REPORT
    update = args.flag("--update", "-u")
    (what,) = args.positionals("gout addons [examples [-u]]   (examples: copy the example addons into the folder;"
                               " -u: replace ones that differ, keeping a .bak)", 0, 1) or [None]
    folder = addon_dir()
    if what == "examples":
        print(f"addons {folder}")
        copy_examples(folder, update)
        return
    if what is not None:
        die("usage: gout addons [examples]")
    effects()
    state = "" if folder.is_dir() else "  (does not exist yet: gout addons examples makes it, with the examples in)"
    print(f"addons {folder}{state}")
    if not REPORT and folder.is_dir():
        print("       no addon files there; gout addons examples copies the ones that come with gout")
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
        target.write_text(default_document(), encoding="utf-8")
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
