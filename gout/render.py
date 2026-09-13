"""Text pictures: the timeline, the cheat sheet, the track panel."""
from __future__ import annotations

import math
import textwrap

from .core import CELL_TRIMMED, CELL_ZERO, fmt_short, MASTER_WAV
from .media import envelope_char
from .model import audible, is_heard
from .fx import Effect, effect, effects, FxContext, GUTTER


def blank_picture(head: str, height: int) -> list[tuple[str, str, str]]:
    return [(head, "", "head")] + [("", "", "graph")] * height + [("", "", "axis")]


def effect_picture(project: "Project", t: dict, eff: Effect, item: dict | None, width: int,
                   height: int) -> list[tuple[str, str, str]]:
    """One effect's picture for a track (or the master); its settings or none when missing."""
    params = None
    if item is not None:
        try:
            params = eff.read(item["params"])
        except ValueError:
            params = None
    rows = eff.picture(FxContext(project, t), params, width, height)
    if not rows:
        rows = blank_picture(f"{eff.name} {item['params'] if item else 'none'}", height)
    if item is not None and not item["on"]:
        rows = [(rows[0][0] + "  (off)", rows[0][1], rows[0][2])] + list(rows[1:])
    return rows


def render_panel(project: "Project", t: dict, width: int, height: int, kind: str) -> list[tuple[str, str, str]]:
    """The track panel: the first effect of `kind` on the track, and the next effect in its
    chain beside it when there is room for both pictures."""
    eff = effect(kind)
    if eff is None:
        return blank_picture(f"{kind}: no such effect installed", height)
    items = t.get("fx", [])
    at = next((i for i, it in enumerate(items) if it["kind"] == kind), None)
    item = items[at] if at is not None else None
    neighbour = None
    if at is not None:
        for it in items[at + 1:]:
            other = effect(it["kind"])
            if other is not None:
                neighbour = (other, it)
                break
    lo, hi = eff.picture_width
    if neighbour and width >= lo + neighbour[0].picture_width[0] + 2:
        right_w = neighbour[0].picture_width[0]
        left_w = min(hi, width - right_w - 2)
        right_w = min(neighbour[0].picture_width[1], width - left_w - 2)
        left = effect_picture(project, t, eff, item, left_w, height)
        right = effect_picture(project, t, neighbour[0], neighbour[1], right_w, height)
        count = max(len(left), len(right))
        left += [("", "", "graph")] * (count - len(left))
        right += [("", "", "graph")] * (count - len(right))
        rows = [(left[0][0][:left_w].ljust(left_w) + "  " + right[0][0], "", "head")]
        for (lt, lc, kind_), (rt, rc, _) in zip(left[1:], right[1:]):
            rows.append((lt[:left_w].ljust(left_w) + "  " + rt, lc[:left_w].ljust(left_w) + "  " + rc, kind_))
        return rows
    return effect_picture(project, t, eff, item, min(width, hi), height)


LABEL_W = 15  # " n name      MS"


TICK_STEPS = (100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
              600000, 900000, 1800000, 3600000, 7200000, 18000000)


def render_timeline(project: Project, width: int, styled: bool = False,
                    playhead_ms: int | None = None) -> list[tuple[str, str, str, str]]:
    """Rows of (label, cells, kind, classes) for a timeline `width` columns wide.

    kind is axis, ruler, track, master or note. Every column of a track shows the
    peak level of that slice of audio as a block ▁▂▃▄▅▆▇█ (6 dB per step). classes
    marks each cell: a audible, s audible but muted or not soloed, t soft-trimmed
    away, z the zero line, m master. Plain output draws trimmed material as ░;
    `styled` (the ui) draws its envelope too and dims it.
    """
    tracks = project.tracks()
    tw = max(10, width - LABEL_W - 1)
    master_ms = int(project.get("master_ms") or 0) if project.master.exists() else 0
    if not tracks:
        return [("", "no tracks yet — add FILE", "note", "")]

    t0 = min(0, min(t["offset_ms"] for t in tracks))
    t1 = max(max(t["offset_ms"] + t["length_ms"] for t in tracks), master_ms, t0 + 1000)
    scale = tw / (t1 - t0)  # columns per millisecond

    def col(ms: int) -> int:
        return int((ms - t0) * scale)

    def cols(a: int, b: int) -> tuple[int, int]:
        start = max(0, min(tw - 1, col(a)))
        end = max(start + 1, min(tw, math.ceil((b - t0) * scale)))
        return start, end

    def paint(cells: list[str], classes: list[str], env: bytes, offset: int, lo: int, hi: int,
              first: int, last: int, cls: str) -> None:
        """Envelope blocks for columns first..last-1, clipped to file time lo..hi."""
        for c in range(first, last):
            lo_ms = max(lo, t0 + c / scale - offset)
            hi_ms = min(hi, t0 + (c + 1) / scale - offset)
            cells[c] = envelope_char(env, lo_ms, hi_ms)
            classes[c] = cls

    step = next((s for s in TICK_STEPS if s * scale >= 9), TICK_STEPS[-1])
    decimals = 0 if step >= 1000 else (2 if step == 250 else 1)
    labels, ruler = [" "] * tw, ["─"] * tw
    last_end = -1
    tick = math.ceil(t0 / step) * step
    while tick <= t1:
        c = col(tick)
        if 0 <= c < tw:
            ruler[c] = "┼"
            text = fmt_short(tick, decimals)
            if c > last_end and c + len(text) <= tw:
                labels[c:c + len(text)] = list(text)
                last_end = c + len(text)
        tick += step
    zero = col(0) if t0 < 0 else -1
    rows = [("", "".join(labels), "axis", ""), ("", "".join(ruler), "ruler", "")]

    any_solo = any(t["solo"] for t in tracks)
    for t in tracks:
        cells, classes = [" "] * tw, [" "] * tw
        if 0 <= zero < tw:
            cells[zero], classes[zero] = CELL_ZERO, "z"
        env = project.envelope(t["file"], project.tracks_dir / t["file"])
        off, length = t["offset_ms"], t["length_ms"]
        fs, fe = cols(off, off + length)
        if styled:
            paint(cells, classes, env, off, 0, length, fs, fe, "t")
        else:
            cells[fs:fe], classes[fs:fe] = [CELL_TRIMMED] * (fe - fs), ["t"] * (fe - fs)
        a, b = audible(t)
        if b > a:
            s_, e_ = cols(off + a, off + b)
            paint(cells, classes, env, off, a, b, s_, e_, "a" if is_heard(t, any_solo) else "s")
        flags = ("M" if t["mute"] else " ") + ("S" if t["solo"] else " ")
        rows.append((f"{t['n']:>2} {t['name'][:9]:<9} {flags}", "".join(cells), "track", "".join(classes)))

    label = f"   {MASTER_WAV}"[:LABEL_W].ljust(LABEL_W)
    if master_ms:
        cells, classes = [" "] * tw, [" "] * tw
        env = project.envelope(MASTER_WAV, project.master)
        s_, e_ = cols(0, master_ms)
        paint(cells, classes, env, 0, 0, master_ms, s_, e_, "m")
        rows.append((label, "".join(cells), "master", "".join(classes)))
    else:
        rows.append((label, "not rendered — mix", "note", ""))
    if playhead_ms is not None and t0 <= playhead_ms <= t1:
        c = max(0, min(tw - 1, col(playhead_ms)))
        marked = []
        for label_, cells, kind, classes in rows:
            if kind in ("ruler", "track", "master") and len(cells) == tw:
                cells = cells[:c] + (cells[c] if cells[c].strip() else "│") + cells[c + 1:]
                classes = classes.ljust(tw)[:c] + "p" + classes.ljust(tw)[c + 1:]
            marked.append((label_, cells, kind, classes))
        rows = marked
    return rows


CHEAT_TEMPLATE = """\
CHEAT SHEET          long short  tab (empty line): next page
TRACKS
 add   a  FILE.. [-a TIME] [-n NAME]  copy into master/
 scan  sc                             new files in master/
 ls    l                              list the tracks
 move  m  TRACK +1s | -500ms | 1:30   later|earlier|place
 trim  t  TRACK -st 2s -et 1:40       soft: file untouched
 trim  t  TRACK -H [-st ..] [-r]      hard: rewrite the file
 trim  t  TRACK -c                    soft trim off
 rm    r  TRACK [-D]                  drop; -D deletes file
MIXER
 mute  mu TRACK [on|off]              mute all off
 solo  s  TRACK [on|off]              solo all off
 gain  g  TRACK -6                    dB, -60 .. +24
 pan   p  TRACK L30 | R30 | C         all start at C
{EFFECTS} mix   x  [-3] [-v]                   -3 also master.mp3
 play  pl [FROM]                      hear master.wav
PROJECT
 undo  u                              not hard trim / rm -D
 view  v  [-w COLS]                   print the timeline
 saveas sa NAME|PATH                  copy the project
 stems sm [DIR] [-A]                  one wav per track
 dump  dp                             the state as json
 import im FILE.json [-s|-t]          apply such a json
 rebuild rb [-f]                      db from master/ + json
 set   se KEY VALUE                   alone: list settings
 stats st                             LUFS / dBTP per track
 new   n  NAME [-R HZ]                48000 Hz by default
 cheat c  sheet on/off  help h        help all: whole page
 quit  q  leave the ui  clear cl      empty the log
 split sp 50 | +5 | -5                left pane width (ui)
 sheet sh (or ctrl-e)                 parameters as a table
 addons   the addon folder and what loaded
MASTER set KEY VALUE
 lufs -14|off  ceiling -1  gain -3    loudness, dBTP, gain
 eq master hp30  reverb master room   any effect, on master
 fadein 1s  fadeout 3s  head 1s  tail 2s
 bits 32f|24|16  mp3 320k|v0  title artist album year
FLAGS  -N --no-mix skip the re-mix    -p DIR the project
       -a --at  -n --name  -H --hard  -c --clear
       -r --reencode  -D --delete  -3 --mp3  -R --rate
       -w --width  -v --verbose
TIMES  2s  500ms  1:30  00:01:30.250  bare number = MINUTES
       trim times count from the track file's start
TRACK  number from ls, or the name (unique prefix ok)
LINE   ← → home end  edit it          ↑ ↓  earlier commands
       tab  complete a command, track, preset or file name
       → at the end of the line  take the grey suggestion
       ctrl-w  delete a word   esc  clear the line
PLAY   space (empty line)  play / stop, playhead stays
       ← → (empty line)  move the playhead 5 s   stop  to 0
KEYS   ctrl-u  timeline on/off   ctrl-k  sheet on/off
       tab shift-tab (empty line)  flip the cheat sheet
       ctrl-n ctrl-p  sheet line  pgup pgdn      scroll log
       ctrl-l  clear the log
       ctrl-← ctrl-→  move the split  (shift/alt too)
       ctrl-g  effect panel on/off  (eq N, comp N.. pick)
       ctrl-d  ctrl-c  quit
SHEET  ↑↓ rows, type the new value, ctrl-s apply and stay
       ctrl-x apply and close  esc close  ctrl-w clear cell
       ctrl-shift-s save as (or: saveas NAME at the prompt)
"""




def cheat_text() -> str:
    lines: list[str] = []
    for eff in effects().values():
        lines += eff.cheat_lines()
    block = """EFFECTS  in order per track; TRACK can be master
 fx    f  TRACK                       the chain, numbered
 fx    f  TRACK add KIND [SETTINGS]   add at the end
 fx    f  TRACK N SETTINGS|on|off|rm  change slot N
 fx    f  TRACK N move M              reorder
 KIND     TRACK SETTINGS|PRESET|off  its first one there
""" + "\n".join(lines) + "\n"
    return CHEAT_TEMPLATE.replace("{EFFECTS}", "", 1).replace("MASTER set KEY VALUE", block + "MASTER set KEY VALUE", 1)


def render_cheat(width: int) -> list[str]:
    lines: list[str] = []
    for line in cheat_text().rstrip("\n").splitlines():
        if len(line) <= width:
            lines.append(line)
        else:
            lines.extend(part.rstrip() for part in textwrap.wrap(
                line, max(20, width), subsequent_indent="           ", replace_whitespace=False))
    return lines
