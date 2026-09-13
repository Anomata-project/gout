"""Text pictures: the timeline, the cheat sheet, the track panel."""
from __future__ import annotations

import math
import textwrap

from .core import CELL_ZERO, fmt_db, fmt_pan, fmt_short, MASTER_WAV
from .media import ENV_RATE
from .model import audible, is_heard
from .fx import Effect, effect, effects, FxContext, GUTTER
from .settings import setting
from .theme import load_theme


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


BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))  # [dot column][dot row]
WAVE_CLASSES = {"master": "m", "muted": "s", "trimmed": "t", "silent": "c"}


def track_class(index: int, palette_size: int) -> str:
    """The class character for the index-th track's colour: '0', '1', ..."""
    return chr(0x30 + index % max(1, palette_size))


def wave_rows(columns: list[list[tuple[float, float, str] | None]], height: int, style: str) -> list[tuple[str, str]]:
    """Draw a waveform `height` rows tall. columns: per character cell, per dot column (2 for
    braille, 1 for blocks), None where there is no audio, else (up, down, class) with up and down
    in 0..1 of half the height. Returns (cells, classes) per row."""
    per_row = 4 if style == "braille" else 2
    half = per_row * height // 2
    rows_cells = [[" "] * len(columns) for _ in range(height)]
    rows_classes = [[" "] * len(columns) for _ in range(height)]
    for c, dots in enumerate(columns):
        lit: set[tuple[int, int]] = set()  # (dot column, dot row from the top)
        cls = " "
        for x, spec in enumerate(dots):
            if spec is None:
                continue
            up, down, kind = spec
            if kind != "c" or cls == " ":
                cls = kind
            n_up = max(1, math.ceil(up * half - 1e-9))
            n_down = max(1, math.ceil(down * half - 1e-9))
            for y in range(half - min(n_up, half), half):
                lit.add((x, y))
            for y in range(half, half + min(n_down, half)):
                lit.add((x, y))
        if not lit:
            continue
        for r in range(height):
            if style == "braille":
                bits = 0
                for (x, y) in lit:
                    if y // 4 == r:
                        bits |= BRAILLE_BITS[x][y % 4]
                if bits:
                    rows_cells[r][c] = chr(0x2800 + bits)
                    rows_classes[r][c] = cls
            else:
                top, bottom = (0, 2 * r) in lit, (0, 2 * r + 1) in lit
                if top or bottom:
                    rows_cells[r][c] = "█" if top and bottom else ("▀" if top else "▄")
                    rows_classes[r][c] = cls
    return [("".join(rc), "".join(rk)) for rc, rk in zip(rows_cells, rows_classes)]


def fit_layout(theme: dict, tracks: int, max_rows: int | None) -> tuple[int, int, int, int]:
    """(master rows, track rows, gap rows, tracks shown) that fit max_rows, shrinking in steps:
    tracks to one row, the master to one row, no gaps, then fewer tracks."""
    mh, th, gap = theme["master_height"], theme["track_height"], theme["gap_rows"]

    def need(mh_, th_, gap_, k):
        return 2 + mh_ + (gap_ if k else 0) + k * th_ + max(0, k - 1) * gap_

    if not max_rows:
        return mh, th, gap, tracks
    for mh_, th_, gap_ in ((mh, th, gap), (mh, 1, gap), (1, 1, gap), (1, 1, 0)):
        if need(mh_, th_, gap_, tracks) <= max_rows:
            return mh_, th_, gap_, tracks
    k = tracks
    while k > 0 and need(1, 1, 0, k) + 1 > max_rows:  # one row for "+N more"
        k -= 1
    return 1, 1, 0, k


def render_timeline(project: "Project", width: int, styled: bool = False, playhead_ms: int | None = None,
                    max_rows: int | None = None, theme: dict | None = None) -> list[tuple[str, str, str, str, str]]:
    """Rows of (label, cells, kind, classes, label role) for a timeline `width` columns wide.

    The master comes first, then every track, each a waveform some rows tall with a gap row
    between them (color.json sets the heights, the gap and braille or blocks). Kinds: axis,
    ruler, wave, gap, note. Cell classes: m master, a digit for a track's palette colour, s muted
    or not soloed, t soft-trimmed away (styled only; plain output leaves it out), c silence and
    the zero line, r ruler, l ruler labels, g gap, p playhead. Label roles are theme keys.
    """
    theme = theme or load_theme(project.root)[0]
    tracks = project.tracks()
    tw = max(10, width - LABEL_W - 1)
    if not tracks:
        return [("", "no tracks yet — add FILE", "note", "", "")]
    style = theme["wave_style"]
    per_cell = 2 if style == "braille" else 1
    scale_db = theme["wave_scale"] == "db"
    palette = len(theme["track_palette"])
    master_ms = int(project.get("master_ms") or 0) if project.master.exists() else 0
    head_ms = int(setting(project, "head"))

    t0 = min(0, min(t["offset_ms"] for t in tracks))
    t1 = max(max(t["offset_ms"] + t["length_ms"] for t in tracks), master_ms - head_ms, t0 + 1000)
    scale = tw / (t1 - t0)  # columns per millisecond
    dot_ms = 1 / (scale * per_cell)

    def col(ms: float) -> int:
        return int((ms - t0) * scale)

    def level(value: int) -> float:
        v = min(value, 128) / 128
        if not scale_db:
            return v
        return max(0.0, 1 + 20 * math.log10(v) / 48) if v > 0 else 0.0

    def columns_for(wave: bytes, offset: int, length: int, a: int, b: int, cls: str, show_trimmed: bool):
        """Dot columns across the timeline for audio placed at offset; a..b is what is heard."""
        windows = len(wave) // 2
        out = []
        for c in range(tw):
            dots = []
            for x in range(per_cell):
                t_lo = t0 + (c * per_cell + x) * dot_ms - offset
                t_hi = t_lo + dot_ms
                if t_hi <= 0 or t_lo >= length:
                    dots.append(None)
                    continue
                heard = a < t_hi and t_lo < b
                if not heard and not show_trimmed:
                    dots.append(None)
                    continue
                kind = cls if heard else "t"
                i0 = max(0, int(max(t_lo, 0) * ENV_RATE / 1000))
                i1 = max(i0 + 1, min(windows, math.ceil(min(t_hi, length) * ENV_RATE / 1000)))
                if i0 >= windows:
                    dots.append((0.0, 0.0, "c" if heard else "t"))
                    continue
                up = max(wave[2 * i0:2 * i1:2], default=0)
                down = max(wave[2 * i0 + 1:2 * i1:2], default=0)
                if max(up, down) <= 1 and heard:
                    kind = "c"  # silence: just the centre line
                dots.append((level(up), level(down), kind))
            out.append(dots)
        return out

    mh, th, gap, shown = fit_layout(theme if master_ms else {**theme, "master_height": 1}, len(tracks), max_rows)

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
    rows = [("", "".join(labels), "axis", "l" * tw, ""),
            ("", "".join(ruler), "ruler", "r" * tw, "")]
    zero = col(0) if t0 < 0 else -1

    def add_group(label_lines: list[tuple[str, str]], drawn: list[tuple[str, str]]) -> None:
        for r, (cells, classes) in enumerate(drawn):
            if 0 <= zero < tw and cells[zero] == " ":
                cells, classes = cells[:zero] + CELL_ZERO + cells[zero + 1:], classes[:zero] + "c" + classes[zero + 1:]
            text, role = label_lines[r] if r < len(label_lines) else ("", "")
            rows.append((text[:LABEL_W].ljust(LABEL_W), cells, "wave", classes, role))

    def add_gap() -> None:
        char = theme["gap_char"] or " "
        for _ in range(gap):
            rows.append(("", char * tw, "gap", "g" * tw, ""))

    # the master, first
    if master_ms:
        wave = project.wave(MASTER_WAV, project.master)
        loud = project.get("master_lufs")
        lines = [(" master", "master_label"), (f"  {float(loud):.1f} LUFS" if loud else "", "ruler_labels")]
        add_group(lines, wave_rows(columns_for(wave, -head_ms, master_ms, 0, master_ms, "m", False), mh, style))
    else:
        rows.append((" master".ljust(LABEL_W), "not rendered yet: mix, or space to play", "note", "", "master_label"))
    if shown:
        add_gap()

    any_solo = any(t["solo"] for t in tracks)
    for i, t in enumerate(tracks[:shown]):
        if i:
            add_gap()
        wave = project.wave(t["file"], project.tracks_dir / t["file"])
        a, b = audible(t)
        cls = track_class(i, palette) if is_heard(t, any_solo) else "s"
        flags = ("M" if t["mute"] else " ") + ("S" if t["solo"] else " ")
        strip = " ".join(part for part in (fmt_db(t["gain_db"]) if t["gain_db"] else "",
                                           fmt_pan(t["pan"]) if abs(t["pan"]) >= 0.005 else "") if part)
        lines = [(f"{t['n']:>2} {t['name'][:9]:<9} {flags}", "track_label"), (f"   {strip}", "ruler_labels")]
        drawn = wave_rows(columns_for(wave, t["offset_ms"], t["length_ms"], a, b, cls, styled), th, style)
        add_group(lines, drawn)
    if shown < len(tracks):
        rows.append(("", f"+{len(tracks) - shown} more tracks, not enough room — ls lists them", "note", "", ""))

    if playhead_ms is not None and t0 <= playhead_ms <= t1:
        c = max(0, min(tw - 1, col(playhead_ms)))
        marked = []
        for label, cells, kind, classes, role in rows:
            if kind in ("ruler", "wave", "gap") and len(cells) == tw:
                char = cells[c] if cells[c].strip() and kind == "wave" else "│"
                cells = cells[:c] + char + cells[c + 1:]
                classes = classes.ljust(tw)[:c] + "p" + classes.ljust(tw)[c + 1:]
            marked.append((label, cells, kind, classes, role))
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
 colors [--init]                      color.json settings
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
