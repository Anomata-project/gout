"""The instrument command, and an instrument track's pattern as text."""
from __future__ import annotations

from .commands import Args
from .core import die, fmt_ms, parse_ms, TRACK_DIR
from .inst import BASE_SETTINGS, Instrument, instruments, resolve_instrument
from .mixer import autorender
from .model import timeline
from .pattern import EMPTY_WORDS, Feature, MAX_STEPS, parse_steps, Pattern
from .sequencer import context, new_track, play, tempo, users, write_track
from .settings import project_bpm

USAGE = ("gout instrument                             the instrument tracks here (ins for short)\n"
         "       gout instrument kinds                       every instrument there is, with its rows and cells\n"
         "       gout instrument add KIND [NAME] [-a TIME]   a new track that plays a pattern: 16 empty steps\n"
         "       gout instrument TRACK                       its pattern: a row per feature, a column per step\n"
         "       gout instrument TRACK ROW STEP VALUE...     cells: note 1 C2, note 5-8 D2, note 1 C2 - C2 D#2 (from step 1 on)\n"
         "       gout instrument TRACK ROW VALUE             the row's value for every step: decay 200 (ROW all VALUE too)\n"
         "       gout instrument TRACK CELL VALUE            the first row: steps 32, steps +4, loop 4, step 1/8, title TEXT, wave saw\n"
         "       a cell that is - (or .) is empty: it takes the row's value; a note can be = (hold) or rest")
EMPTY_MARK, STEPS_A_LINE = "·", 16


def print_kinds() -> None:
    for item in instruments().values():
        origin = "" if item.source == "built-in" else f"   [addon {item.source}]"
        print(f"{item.name:<10} {', '.join(item.aliases):<6} {item.summary}{origin}")
        print(f"{'':<17} rows: {' '.join(f.name for f in item.features)}"
              + (f"   cells: {' '.join(f.name for f in item.settings)}" if item.settings else ""))


def instrument_track(project, spec: str) -> dict:
    """A track that is an instrument's, with the instrument under "item"; dies when it is not one."""
    t = project.audio_track(spec, "instrument")
    if not t.get("instrument"):
        die(f"track {t['n']} {t['name']} is a recording, not an instrument: gout instrument add KIND makes one")
    item = resolve_instrument(t["instrument"]["kind"])
    if item is None:
        die(f"track {t['n']} {t['name']} is played by {t['instrument']['kind']}, which is not installed (an addon?):"
            f" {TRACK_DIR}/{t['file']} sounds as it was last written, and its pattern cannot be changed here")
    return {**t, "item": item}


def headline(project, t: dict, item: Instrument | None) -> str:
    data = t["instrument"]
    steps = data["pattern"].get("steps", 16)
    start, _ = timeline(t)
    if item is None:
        return f"ins   {t['n']:>2}  {t['name']:<16} {data['kind']} (not installed)  {steps} steps  at {fmt_ms(start)}"
    ctx = context(project, item, data["settings"])
    loop = item.value(data["settings"], "loop")
    return (f"ins   {t['n']:>2}  {t['name']:<16} {item.name}  {steps} steps of {item.value(data['settings'], 'step')}"
            f" at {ctx.bpm:g} bpm = {steps * ctx.step_s:.3f} s" + (f", {loop} times" if loop > 1 else "")
            + f"  at {fmt_ms(start)}")


def cell_text(feature: Feature, value) -> str:
    return EMPTY_MARK if value is None else feature.format(value)


def grid_lines(project, t: dict, item: Instrument) -> list[str]:
    """The pattern as text: the first row's cells, then a row per feature in blocks of 16 steps,
    with the row's value for every step in the column before the steps."""
    data = t["instrument"]
    settings = data["settings"]
    pattern = Pattern.from_json(data["pattern"], item.features)
    first = [f"title {settings.get('title') or t['name']}"]
    if settings.get("description"):
        first.append(f"description {settings['description']}")
    first += [f"{f.name} {f.format(item.value(settings, f.name))}" for f in item.settings]
    lines = ["      " + "   ".join(first)]
    reads = item.reads(context(project, item, settings), pattern, settings)
    texts = {f.name: [cell_text(f, pattern.cell(f, s)) for s in range(1, pattern.steps + 1)] for f in item.features}
    texts.update({name: list(row) + [""] * (pattern.steps - len(row)) for name, row in reads})
    names = [f.name for f in item.features] + [name for name, _ in reads]
    every = {f.name: ("" if pattern.rows.get(f.name, {}).get("all") is None and f.kind == "note"
                      else f.format(pattern.every(f))) for f in item.features}
    name_w = max(len(n) for n in names)
    all_w = max([3] + [len(v) for v in every.values()])
    cell_w = max([3] + [len(c) for row in texts.values() for c in row]) + 1
    for lo in range(0, pattern.steps, STEPS_A_LINE):
        hi = min(pattern.steps, lo + STEPS_A_LINE)

        def cells(items: list[str]) -> str:
            groups = [items[i:i + 4] for i in range(0, len(items), 4)]
            return " │".join("".join(f"{c:>{cell_w}}" for c in group) for group in groups)

        lines.append(f"      {'':<{name_w}} {'all' if lo == 0 else '':>{all_w}} │" + cells([str(s) for s in range(lo + 1, hi + 1)]))
        for name in names:
            lines.append(f"      {name:<{name_w}} {every.get(name, '') if lo == 0 else '':>{all_w}} │" + cells(texts[name][lo:hi]))
    return lines


def show(project, t: dict) -> None:
    print(headline(project, t, t["item"]))
    for line in grid_lines(project, t, t["item"]):
        print(line.rstrip())


def set_cells(pattern: Pattern, feature: Feature, words: list[str]) -> str:
    """ROW STEP VALUE..., ROW all VALUE or ROW VALUE. Returns what was done, in words."""

    def read(word: str):
        return None if word.lower() in EMPTY_WORDS else feature.parse(word)

    try:
        if len(words) == 1 or words[0].lower() == "all":  # ROW VALUE, ROW all VALUE
            if len(words) > 2 or (len(words) == 2 and words[0].lower() != "all"):
                raise ValueError(f"{feature.name} all takes one value")
            pattern.put(feature, None, read(words[-1]))
            return f"{feature.name} {cell_text(feature, pattern.rows[feature.name]['all'])} on every step"
        steps = parse_steps(words[0], pattern.steps)
        values = [read(word) for word in words[1:]]
        if len(values) > 1:  # several values run on from the first step named
            if len(steps) != 1:
                raise ValueError(f"several values go to the steps from one step on: {feature.name} {steps[0]} {' '.join(words[1:])}")
            if steps[0] + len(values) - 1 > pattern.steps:
                raise ValueError(f"{len(values)} values from step {steps[0]} run past step {pattern.steps}"
                                 f" (gout instrument TRACK steps +{steps[0] + len(values) - 1 - pattern.steps} makes room)")
            steps = list(range(steps[0], steps[0] + len(values)))
        else:
            values = values * len(steps)
        for step, value in zip(steps, values):
            pattern.put(feature, [step], value)
    except ValueError as exc:
        die(str(exc))
    where = f"{steps[0]}" if len(steps) == 1 else f"{steps[0]}-{steps[-1]}" if steps == list(range(steps[0], steps[-1] + 1)) \
        else ",".join(str(s) for s in steps)
    return f"{feature.name} {where}: {' '.join(cell_text(feature, v) for v in values[:16])}{' …' if len(values) > 16 else ''}"


def set_setting(item: Instrument, settings: dict, pattern: Pattern, feature: Feature, words: list[str]) -> str:
    """A cell of the first row. Returns what was done, in words."""
    text = " ".join(words)
    try:
        if feature.name == "steps":
            if len(words) == 1 and words[0][:1] in "+-" and words[0][1:].isdigit():
                text = str(pattern.steps + int(words[0]))
            steps = feature.parse(text)
            dropped = pattern.resize(steps)
            return f"steps {steps}" + (f"  ({dropped} cell{'s' if dropped != 1 else ''} past the end dropped)" if dropped else "")
        if text.lower() in EMPTY_WORDS:  # the cell goes: the instrument's own value again
            settings.pop(feature.name, None)
        else:
            settings[feature.name] = feature.parse(text)
    except ValueError as exc:
        die(str(exc))
    return f"{feature.name} {feature.format(item.value(settings, feature.name)) or '(none)'}"


def cmd_instrument(project, args: Args) -> None:
    at = args.value("--at", "-a")
    words = args.positionals(USAGE, 0)
    if not words:
        found = users(project)
        for t in found:
            print(headline(project, t, t["item"]))
        if not found:
            print(f"ins   no instrument tracks yet — gout instrument add KIND  ({', '.join(instruments())})")
        return
    if words[0].lower() == "add":
        if not 2 <= len(words) <= 3:
            die(f"usage: {USAGE}")
        item = resolve_instrument(words[1])
        if item is None:
            die(f"no instrument called {words[1]!r}: there is {', '.join(instruments())}")
        project.record(f"instrument add {item.name}" + (f" {words[2]}" if len(words) == 3 else ""))
        if project_bpm(project) is None:
            project.set("bpm", f"{tempo(project):g}")
            print(f"set   bpm {tempo(project):g}  (a pattern needs a tempo; gout set bpm 96 changes it, and the pattern with it)")
        t = new_track(project, item, words[2] if len(words) == 3 else None, parse_ms(at) if at else 0)
        project.created([t["file"]])
        print(headline(project, t, item) + f"  ({TRACK_DIR}/{t['file']}, written from the pattern)")
        autorender(project, args)
        return
    if at:
        die(f"-a goes with add\nusage: {USAGE}")
    t = instrument_track(project, words[0])
    item = t["item"]
    if len(words) == 1:
        show(project, t)
        return
    data = t["instrument"]
    settings = dict(data["settings"])
    try:
        pattern = Pattern.from_json(data["pattern"], item.features)
    except ValueError as exc:
        die(f"{t['name']}: {exc}")
    name, rest = words[1].lower(), words[2:]
    row, cell = item.feature(name), item.setting(name)
    if row is None and cell is None:
        die(f"{item.name} has no row or cell called {words[1]!r}: rows {' '.join(f.name for f in item.features)};"
            f" cells {' '.join(f.name for f in (*BASE_SETTINGS, *item.settings))}")
    if not rest:
        die(f"{name} what?\nusage: {USAGE}")
    done = set_cells(pattern, row, rest) if row is not None else set_setting(item, settings, pattern, cell, rest)
    if pattern.steps > MAX_STEPS:
        die(f"a pattern has {MAX_STEPS} steps at most")
    changed = {"kind": item.name, "settings": settings, "pattern": pattern.to_json()}
    chans, _ = play(project, {**t, "instrument": changed}, item)  # before anything is stored: a pattern it cannot play changes nothing
    project.record(f"instrument {t['name']} {' '.join(words[1:])}")
    project.instrument_set(t["file"], item.name, settings, changed["pattern"])
    length_ms = write_track(project, {**t, "instrument": changed}, item, chans)
    print(f"ins   {t['n']:>2}  {t['name']:<16} {done}   ({fmt_ms(length_ms)})")
    autorender(project, args)
