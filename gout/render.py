"""Text pictures: the timeline, the cheat sheet, the track panel."""
from __future__ import annotations

import math
import textwrap

from .core import CELL_ZERO, fmt_db, fmt_pan, fmt_short, MASTER_WAV
from .media import ENV_RATE
from .model import audible, is_heard, part_label, part_start, timeline
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


def panel_head(t: dict, kind: str) -> str:
    """The effect panel's name line without its pictures: the effect and its settings, and the
    one after it in the chain, the pair the pictures would show. Cheap: nothing is drawn."""
    items = t.get("fx", [])
    at = next((i for i, it in enumerate(items) if it["kind"] == kind), None)
    if at is None:
        return f"{kind} none"
    parts = []
    for it in [items[at]] + [it for it in items[at + 1:] if effect(it["kind"]) is not None][:1]:
        eff = effect(it["kind"])
        text = f"{it['kind']} {it['params'] or (eff.empty if eff else '')}".rstrip()
        parts.append(text + ("" if it["on"] else "  (off)"))
    return "   ".join(parts)


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

    t0 = min(0, min(min(t["offset_ms"], timeline(t)[0]) for t in tracks))
    t1 = max(max(max(t["offset_ms"] + t["length_ms"], timeline(t)[1]) for t in tracks), master_ms - head_ms, t0 + 1000)
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

    def parts_columns(t: dict, wave: bytes, cls: str):
        """A track in parts: each part's audio where the part is, muted ones dim; where parts
        overlap, the louder dot shows."""
        merged = None
        for part in t["parts"]:
            drawn = columns_for(wave, t["offset_ms"] + part["shift_ms"], t["length_ms"], part["in_ms"], part["out_ms"],
                                "s" if part["mute"] else cls, False)
            if merged is None:
                merged = drawn
                continue
            for c, dots in enumerate(drawn):
                merged[c] = [new if old is None or (new is not None and new[0] + new[1] > old[0] + old[1]) else old
                             for old, new in zip(merged[c], dots)]
        return merged

    def part_marks(t: dict, row: tuple) -> tuple:
        """The gap row above a track in parts, with a ╷ and the part's name where each part starts."""
        label, cells, kind, classes, role = row
        cells, classes = list(cells), list(classes)
        marks = sorted((col(part_start(t, part)), part_label(t["parts"], part)) for part in t["parts"])
        for k, (c, name) in enumerate(marks):
            if not 0 <= c < tw:
                continue
            room = (marks[k + 1][0] if k + 1 < len(marks) else tw) - c - 1
            text = ("╷" + name)[:max(1, room)]
            for x, ch in enumerate(text):
                if c + x < tw:
                    cells[c + x], classes[c + x] = ch, "l"
        return label, "".join(cells), kind, "".join(classes), role

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
        current = project.master_is_current()
        second = (f"  {float(loud):.1f} LUFS" if loud else "") if current else "  out of date"
        lines = [(" master", "master_label"), (second, "ruler_labels")]
        drawn = columns_for(wave, -head_ms, master_ms, 0, master_ms, "m" if current else "s", False)
        add_group(lines, wave_rows(drawn, mh, style))
    else:
        rows.append((" master".ljust(LABEL_W), "not rendered yet: space plays the project live", "note", "",
                     "master_label"))
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
        if t["parts"]:
            columns = parts_columns(t, wave, cls)
            if gap and rows and rows[-1][2] == "gap":
                rows[-1] = part_marks(t, rows[-1])
        else:
            columns = columns_for(wave, t["offset_ms"], t["length_ms"], a, b, cls, styled)
        add_group(lines, wave_rows(columns, th, style))
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


# ---------------------------------------------------------------------------- cheat sheet
#
# The sheet is data: sections of rows, laid out for whatever width it gets. Command rows are
# (long name, short name, arguments, what it does); key rows are (keys, what they do). Narrow, it
# wraps; wide, nothing is cut; wide enough for two, it flows into two columns.

CHEAT_TITLE = "CHEAT SHEET   long short   tab on an empty line: next page"
CHEAT_COLUMN = 64  # the narrowest a column may be before the sheet goes back to one column
CHEAT_GAP = 3

COMMAND_SECTIONS = [
    ("TRACKS", "", [
        ("add", "a", "FILE... [-a TIME] [-n NAME]", "copy files into master/ (other formats become wav)"),
        ("scan", "sc", "", "register files you put in master/ yourself"),
        ("ls", "l", "", "list the tracks"),
        ("move", "m", "TRACK +1s | -500ms | 1:30", "later, earlier, or place at a time"),
        ("", "", "TRACK PART +1s | 1:30", "a part along its track"),
        ("", "", "1 3 -12s | all -12s", "several tracks or all, one undo"),
        ("trim", "t", "TRACK -st 2s -et 1:40", "soft trim: the file is untouched"),
        ("", "", "TRACK -et -5s", "a minus counts back from the file's end"),
        ("", "", "TRACK PART -st 1:31 -et +2s", "a part, where you hear it; + or - moves an edge"),
        ("trim", "t", "TRACK -H [-st ..] [-r]", "hard trim: rewrite the file"),
        ("trim", "t", "TRACK -c", "soft trim off"),
        ("rm", "r", "TRACK [-D]", "drop the track; -D deletes its file"),
        ("rm", "r", "TRACK PART", "drop a part"),
    ]),
    ("MIXER", "", [
        ("mute", "mu", "TRACK [PART] [on|off]", "mute; mute all off"),
        ("solo", "s", "TRACK [on|off]", "solo; solo all off"),
        ("gain", "g", "TRACK [PART] -6", "dB, -60 to +24"),
        ("pan", "p", "TRACK [PART] L30 | R30 | C", "every track starts at C"),
        ("part", "pt", "TRACK 1:30 | here", "cut into parts p1, p2: gain 3 p2 -10, eq 3 p2 hp80"),
        ("part", "pt", "TRACK PART name NAME", "call a part by a name"),
        ("part", "pt", "TRACK join [PART PART] [-f]", "one piece again; -f drops part settings"),
        ("mix", "x", "[-3] [-v]", "render master.wav; -3 also master.mp3"),
        ("play", "pl", "[FROM] [-r]", "hear it; live when master.wav is out of date"),
        ("record", "rec", "[FROM] [-t 30s] [-n NAME] [-d]", "a new track while the project plays; -d without"),
        ("inputs", "in", "[N | default]", "what can be recorded; N picks one"),
        ("record", "rec", "calibrate [-i INPUT]", "clicks out and back in: takes land on time"),
    ]),
    ("EFFECTS", "in order per track; TRACK can be master", [
        ("fx", "f", "TRACK", "the chain, numbered"),
        ("fx", "f", "TRACK add KIND [SETTINGS]", "add an effect at the end"),
        ("fx", "f", "TRACK N SETTINGS | on | off | rm", "change, bypass or remove slot N"),
        ("fx", "f", "TRACK N move M", "reorder"),
        ("fx", "f", "kinds", "every effect there is"),
        ("KIND", "", "TRACK [PART] SETTINGS | PRESET", "the first effect of that kind, added if missing"),
        ("KIND", "", "TRACK on | off | clear", "bypass, bring back, remove"),
        ("KIND", "", "presets", "every preset with its settings"),
    ]),
    ("PROJECT", "", [
        ("undo", "u", "", "undo the last change, again for the one before (ctrl-u); not past a hard trim or rm -D"),
        ("view", "v", "[-w COLS]", "print the timeline"),
        ("saveas", "sa", "NAME | PATH", "copy the whole project"),
        ("stems", "sm", "[DIR] [-A]", "one wav per track"),
        ("video", "vd", "IMAGE [-o FILE]", "master.mp4 for YouTube: the image and the song"),
        ("video", "vd", "fractal all [-e 10s] [-c IMG]", "the fractal with the song, a new one on a hit"),
        ("dump", "dp", "", "the state as json"),
        ("import", "im", "FILE.json [-s | -t]", "apply such a json: settings, tracks or both"),
        ("rebuild", "rb", "[-f]", "gout.db again from master/ and gout.json"),
        ("set", "se", "KEY VALUE", "a setting; set alone lists them"),
        ("stats", "st", "", "LUFS, LRA and true peak per track"),
        ("new", "n", "NAME [-R HZ]", "a new project, 48000 Hz by default"),
        ("cheat", "c", "", "this sheet on and off"),
        ("help", "h", "[all | COMMAND]", "this sheet in the log; all: the whole instruction page; help record: one command"),
        ("quit", "q", "", "leave the ui"),
        ("clear", "cl", "", "empty the log"),
        ("split", "sp", "50 | +5 | -5", "left pane width in the ui"),
        ("sheet", "sh", "", "every parameter as a table (ctrl-e)"),
        ("colors", "", "[--init [--project]]", "color.json: colours and timeline layout"),
        ("addons", "", "[examples]", "the addon folder and what loaded; examples copies the examples in"),
    ]),
    ("MASTER", "set KEY VALUE", [
        ("lufs", "", "-14 | -23 | off", "loudness target: -14 streaming, -16 Apple, -23 broadcast"),
        ("ceiling", "", "-1", "true-peak ceiling, dBTP"),
        ("gain", "", "-3", "master gain, dB"),
        ("fadein", "", "1s", "fade in; fadeout 3s fades out"),
        ("head", "", "1s", "silence before; tail 2s after"),
        ("bits", "", "32f | 24 | 16", "master.wav format"),
        ("mp3", "", "320k | v0", "mp3 bounce quality"),
        ("title", "", "TEXT", "tags: title, artist, album, year, comment"),
        ("bpm", "", "120", "tempo, for delays in note values"),
        ("autorender", "", "idle | on | off", "when master.wav is rendered"),
        ("eq", "", "hp30", "any effect on the master, as gout eq master hp30"),
    ]),
]

KEY_SECTIONS = [
    ("FLAGS", "", [
        ("-N --no-mix", "no render after this change, when autorender is on"),
        ("-p DIR", "use the project in DIR"),
        ("-a --at  -n --name", "add: where, and under what name"),
        ("-H --hard  -c --clear", "trim: rewrite the file, or soft trim off"),
        ("-r --reencode", "cut exactly; play -r renders first"),
        ("-D --delete", "rm: delete the file too"),
        ("-c --cover  -T --no-title", "video: a cover first; no title"),
        ("-c --channel  -s --stereo", "record: input channel N; stereo from N and N+1"),
        ("-i --in  -t --time", "record: another input once; how long"),
        ("-3 --mp3", "mix: also master.mp3"),
        ("-R --rate", "new: the sample rate"),
        ("-w --width", "view and cheat: columns"),
        ("-v --verbose", "show the ffmpeg commands"),
    ]),
    ("TIMES", "", [
        ("2s 500ms 1:30", "seconds, milliseconds, minutes:seconds"),
        ("00:01:30.250", "hours, minutes, seconds and milliseconds"),
        ("34", "a bare number is MINUTES"),
        ("trim", "times count from the start of the track's own file"),
    ]),
    ("TRACK", "", [
        ("TRACK", "the number from ls, or the name (a unique start will do); master or 0 for effects"),
    ]),
    ("LINE", "", [
        ("← → home end", "move in the line; typing goes in at the cursor"),
        ("↑ ↓", "earlier commands, to change and run again"),
        ("tab", "complete a command, track, preset or file name"),
        ("→ at the end", "take the grey suggestion"),
        ("ctrl-w", "delete a word"),
        ("esc", "clear the line"),
    ]),
    ("PLAY", "", [
        ("space", "on an empty line: play and stop; the playhead stays"),
        ("← →", "on an empty line: move the playhead 5 s"),
        ("stop", "at the prompt: the playhead back to the start"),
    ]),
    ("KEYS", "", [
        ("ctrl-u", "undo the last change"),
        ("ctrl-t", "timeline on and off"),
        ("ctrl-k", "cheat sheet on and off"),
        ("ctrl-g", "effect pictures on and off; the name line stays (eq N, comp N pick the track)"),
        ("ctrl-e", "the parameter sheet"),
        ("tab shift-tab", "on an empty line: flip the cheat sheet"),
        ("ctrl-n ctrl-p", "the cheat sheet a line at a time"),
        ("pgup pgdn", "scroll the log"),
        ("ctrl-l", "clear the log"),
        ("ctrl-← ctrl-→", "move the split between the panes (shift or alt too)"),
        ("ctrl-d ctrl-c", "quit"),
    ]),
    ("SHEET", "", [
        ("↑ ↓", "rows; type the new value"),
        ("ctrl-s", "apply and stay"),
        ("ctrl-x", "apply and close"),
        ("esc", "close, keeping the edits for next time"),
        ("ctrl-w", "clear the cell"),
        ("ctrl-p", "the row as a command on the prompt (the line at the bottom shows it to copy)"),
        ("ctrl-shift-s", "save as (or saveas NAME at the prompt)"),
    ]),
]

CHEAT_HEADINGS = {name for name, _, _ in COMMAND_SECTIONS + KEY_SECTIONS} | {"CHEAT", "SCREENS"}


def cheat_sections() -> list[tuple[str, str, str, list[tuple]]]:
    """(heading, note, "commands" or "keys", rows), effects from the registry included."""
    out = []
    for heading, note, rows in COMMAND_SECTIONS:
        rows = list(rows)
        if heading == "EFFECTS":
            for eff in effects().values():
                rows += eff.cheat_entries()
        out.append((heading, note, "commands", rows))
    out += [(heading, note, "keys", rows) for heading, note, rows in KEY_SECTIONS]
    from .screens import screens
    found = list(screens().values())
    if found:
        rows = []
        for screen in found:
            opens = " or ".join(filter(None, (screen.key, screen.name)))
            rows.append((opens, screen.summary + ("" if screen.source == "built-in" else " (addon)")))
        rows += [("esc", "back to gout; the song keeps playing"), ("space  ← →", "play and stop, move 5 s")]
        rows += [tuple(pair) for screen in found for pair in screen.help]
        out.append(("SCREENS", "full screen, from addons", "keys", rows))
    return out


def wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, max(8, width), break_on_hyphens=False) or [""]


def cheat_row(long: str, short: str, args: str, what: str, width: int, name_w: int, short_w: int,
              args_w: int) -> list[str]:
    """One command row. Arguments sit in a column of args_w, the description beside them; when
    that does not fit, the description goes under the arguments."""
    prefix_w = 1 + name_w + 1 + short_w
    prefix = f" {long:<{name_w}} {short:<{short_w}}"
    if len(prefix) > prefix_w:
        prefix = f" {long} {short}".rstrip()
    lead = max(len(prefix), prefix_w) + 1            # where the arguments start
    room = width - lead
    desc_at = prefix_w + 1 + args_w + 2              # the description column
    beside = width - desc_at
    if not args:
        if len(prefix) <= prefix_w and (beside >= 26 or len(what) <= beside):
            parts = wrap(what, beside)
            return [prefix.ljust(desc_at) + parts[0]] + [" " * desc_at + part for part in parts[1:]]
        parts = wrap(what, room)
        return [prefix.ljust(lead) + parts[0]] + [" " * lead + part for part in parts[1:]]
    if len(prefix) <= prefix_w and len(args) <= args_w and beside >= 12:
        column = wrap(what, beside)
        if len(column) > 1 and beside < 26:  # no slivers: a narrow column only for one line
            column = column * 99
        stacked = len(wrap(args, room)) + len(wrap(what, room - 2))
        if len(column) <= stacked:
            return ([prefix.ljust(lead) + args.ljust(args_w + 2) + column[0]]
                    + [" " * desc_at + part for part in column[1:]])
    arg_parts = wrap(args, room)
    lines = [prefix.ljust(lead) + arg_parts[0]] + [" " * lead + part for part in arg_parts[1:]]
    return lines + [" " * (lead + 2) + part for part in wrap(what, room - 2)]


def cheat_section_lines(heading: str, note: str, kind: str, rows: list[tuple], width: int,
                        name_w: int, short_w: int) -> list[str]:
    lines = wrap(f"{heading}  {note}".rstrip(), width)
    lines[1:] = [" " * (len(heading) + 2) + part.strip() for part in lines[1:]]
    if kind == "keys":
        key_w = min(max(len(r[0]) for r in rows), max(6, width // 3))
        for keys, what in rows:
            if len(keys) > key_w:
                lines.append(" " + keys)
                lines += [" " * (key_w + 3) + part for part in wrap(what, width - key_w - 3)]
            else:
                parts = wrap(what, width - key_w - 3)
                lines.append(f" {keys:<{key_w}}  {parts[0]}")
                lines += [" " * (key_w + 3) + part for part in parts[1:]]
        return [line[:width] for line in lines]
    room = width - (1 + name_w + 1 + short_w + 1)
    best = None
    for args_w in sorted({len(r[2]) for r in rows if len(r[2]) <= room - 18} | {0}):
        body = [line for r in rows for line in cheat_row(*r, width, name_w, short_w, args_w)]
        if best is None or len(body) < len(best):  # fewest lines; on a tie the narrower column
            best = body
    return [line[:width] for line in lines + best]


def cheat_layout(width: int) -> tuple[list[str], int | None]:
    """The sheet's lines for this width, and where the second column starts (None for one)."""
    width = max(30, width)
    two = width >= 2 * CHEAT_COLUMN + CHEAT_GAP
    col_w = (width - CHEAT_GAP) // 2 if two else width
    sections = cheat_sections()
    commands = [r for _, _, kind, rows in sections if kind == "commands" for r in rows]
    name_w = min(10, max(len(r[0]) for r in commands), max(4, col_w // 8))
    short_w = min(4, max(len(r[1]) for r in commands))
    blocks = [cheat_section_lines(h, n, k, rows, col_w, name_w, short_w) for h, n, k, rows in sections]
    title = [" ".join(wrap(CHEAT_TITLE, width)[0].split(" ")).rstrip()] if width >= len(CHEAT_TITLE) else ["CHEAT SHEET"]
    if not two:
        return title + [line for block in blocks for line in block], None
    total = sum(len(b) for b in blocks)
    best, split = None, 1
    running = 0
    for i, block in enumerate(blocks[:-1], 1):  # keep sections in order; balance the heights
        running += len(block)
        height = max(running, total - running)
        if best is None or height < best:
            best, split = height, i
    left = [line for block in blocks[:split] for line in block]
    right = [line for block in blocks[split:] for line in block]
    rows = []
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        rows.append((l.ljust(col_w) + " " * CHEAT_GAP + r).rstrip())
    return title + rows, col_w + CHEAT_GAP


def render_cheat(width: int) -> list[str]:
    return cheat_layout(width)[0]


def cheat_text() -> str:
    return "\n".join(render_cheat(100)) + "\n"


