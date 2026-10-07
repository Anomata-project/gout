"""bonepipe from the shell: python3 -m bonepipe ..."""
from __future__ import annotations

import random
import sys
import textwrap

from . import geometry, objects, scale, sound

USAGE = """bonepipe: what a pipe sounds like, computed from its geometry

  python3 -m bonepipe objects                         the objects there are
  python3 -m bonepipe show OBJECT                     what was measured on it: value, method, source, page, confidence
  python3 -m bonepipe bore OBJECT [edge_mm=0.7]       its marrow cavity along the bone, and where the holes are
  python3 -m bonepipe unknowns OBJECT                 everything a reconstruction has to decide, with its range
  python3 -m bonepipe notes OBJECT [NAME=VALUE ...]   one reconstruction: its fingerings, their notes, the intervals
  python3 -m bonepipe spread OBJECT [-n 200] [-s 1] [NAME=VALUE ...]
                                                      the same over the whole range of what is not known
  python3 -m bonepipe wav OBJECT FILE.wav [NAME=VALUE ...]
                                                      hear one reconstruction: its plain fingerings, a breath each
  python3 -m bonepipe check                           that every number in the data has its source

NAME=VALUE fixes one unknown: blown=distal far=closed hole5=yes dist_extra_mm=30 mouth_open=0.05"""


def die(text: str) -> "NoReturn":  # noqa: F821
    print(f"bonepipe: {text}", file=sys.stderr)
    raise SystemExit(1)


def given(thing, words: list[str]) -> dict:
    """NAME=VALUE words as values for a reconstruction."""
    out = {}
    for word in words:
        name, eq, text = word.partition("=")
        if not eq:
            die(f"{word!r}: expected NAME=VALUE\n{USAGE}")
        if name in geometry.CHOICES:
            options = geometry.CHOICES[name]
            if options == (True, False):
                if text.lower() not in ("yes", "no", "on", "off", "true", "false"):
                    die(f"{name} is yes or no, not {text!r}")
                out[name] = text.lower() in ("yes", "on", "true")
            elif text not in options:
                die(f"{name} is one of {', '.join(options)}, not {text!r}")
            else:
                out[name] = text
        elif name in geometry.NUMBERS:
            try:
                out[name] = float(text)
            except ValueError:
                die(f"{name} is a number, not {text!r}")
        else:
            die(f"nothing called {name!r}: python3 -m bonepipe unknowns {thing.id} lists them")
    return out


def cmd_objects() -> None:
    for name in objects.object_ids():
        thing = objects.load(name)
        date = thing.data.get("date") or {}
        when = f"{date['from_ka']:g} to {date['to_ka']:g} thousand years" if date else "undated"
        print(f"{thing.id:<16} {thing.name}  ({when}; {thing.data.get('status', '')})")


def cmd_show(thing) -> None:
    known = objects.sources()
    d = thing.data
    print(f"{thing.id}  {thing.name}")
    print(f"  {d['material']}; kept at {d['kept_at']}")
    print(f"  status: {d['status']}. {d['status_note']}")
    site, date = d["site"], d["date"]
    print(f"  found at {site['name']}, {site['country']} ({site['lat']}, {site['lon']}; {site['source']}, {site['confidence']})")
    print(f"  {date['from_ka']:g} to {date['to_ka']:g} thousand years: {date['method']} ({date['source']} p. {date['page']}, {date['confidence']})")
    print()
    print(f"  {'what':<36} {'value':>10}  {'method':<9} {'source':<15} {'page':<6} {'sure':<7} note")
    for m in thing.measurements:
        value = "-".join(f"{v:g}" for v in m["value"]) if isinstance(m["value"], list) else f"{m['value']:g}"
        print(f"  {m['what']:<36} {value + ' ' + m['unit']:>10}  {m['method']:<9} {m['source']:<15} {m['page']:<6} {m['confidence']:<7} {m.get('note', '')}")
    print()
    for fact in d.get("facts", []):
        print(f"  {fact['what']}  ({fact['source']} p. {fact['page']}, {fact['confidence']})")
    print()
    print("  how it has been blown:")
    for way in d.get("blown", []):
        print(f"    {way['who']}: {way['how']}  ({way['source']} p. {way['page']})")
    print()
    print("  sources")
    used = sorted({m["source"] for m in thing.measurements} | {f["source"] for f in d.get("facts", [])}
                  | {w["source"] for w in d.get("blown", [])} | {site["source"], date["source"]})
    for key in used:
        s = known[key]
        print(f"    {key:<22} {s['authors']} {s['year']}. {s['title']}. {s.get('in', '')}  {s['url']}")


def cmd_bore(thing, words: list[str]) -> None:
    edge = given(thing, words).get("edge_mm", geometry.NUMBERS["edge_mm"][0])
    found = geometry.stations(thing, edge)
    data = thing.slices()
    print(f"{thing.id}  the marrow cavity from {len(data['slices'])} CT slices ({data['source']}, {data['figures']})")
    print(f"  every surface moved by edge_mm = {edge:g}: {geometry.NUMBERS['edge_mm'][4]}")
    print(f"  {'mm from the proximal end':<26} {'slice':>5} {'area mm2':>9} {'radius mm':>10}  basis")
    last = None
    for s in found:
        if last is not None and s["at_mm"] - last > 1.01:
            print(f"  {'':<26} {'':>5} {'':>9} {'':>10}  (no slice printed, or a hole's edge: the bore runs straight across)")
        print(f"  {s['at_mm']:<26.1f} {s['slice']:>5} {s['area_mm2']:>9.1f} {s['radius_mm']:>10.2f}  {s['basis']}")
        last = s["at_mm"]
    print()
    for name, (lo, hi) in geometry.openings(thing).items():
        if name in ("hole1", "hole2"):
            print(f"  {name}: the wall is open from {lo:g} to {hi:g} mm in the silhouettes, the middle at {(lo + hi) / 2:g} mm")
        elif name == "hole3":
            print(f"  {name}: its distal edge is at {hi:g} mm, where the ring closes; the rest is broken away")
        else:
            print(f"  {name}: its proximal edge is at {lo:g} mm, where the ring opens on the anterior side; the rest is broken away")
    measured = [s for s in found if s["basis"] == "measured"]
    print(f"\n  measured where the bone is a closed ring: {measured[0]['at_mm']:g} to {measured[-1]['at_mm']:g} mm"
          f" ({len(measured)} slices); estimated from the width between the walls elsewhere")


def cmd_unknowns(thing) -> None:
    lim, base = geometry.limits(thing), geometry.defaults(thing)
    print(f"{thing.id}  what a reconstruction has to decide\n")
    for key, (_, _, _, unit, what) in geometry.NUMBERS.items():
        lo, hi = lim[key]
        print(f"  {key:<14} {base[key]:>6g} {unit:<3} {lo:g} to {hi:g}")
        for line in textwrap.wrap(what, 88):
            print(f"  {'':<14} {line}")
    for key, options in geometry.CHOICES.items():
        shown = ["yes" if o is True else "no" if o is False else o for o in options]
        start = "yes" if base[key] is True else "no" if base[key] is False else base[key]
        print(f"  {key:<14} {start:>6}     one of {', '.join(shown)}")


def note_line(note, before=None) -> str:
    if note.hz is None:
        return f"  {note.text:<10} {'no resonance in reach':>24}"
    line = f"  {note.text:<10} {note.hz:>8.1f} Hz  {scale.pitch_name(note.hz):<8} q {note.q:>3.0f}"
    if before is not None and before.hz:
        step = scale.cents(note.hz, before.hz)
        p, q, off = scale.nearest_ratio(abs(step))
        line += f"   {step:>+6.0f} c   {p}/{q} {off:+.0f} c"
    return line


def cmd_notes(thing, words: list[str]) -> None:
    try:
        recon = geometry.Reconstruction(thing, **given(thing, words))
    except ValueError as exc:
        die(str(exc))
    setup = geometry.build(recon)
    lo, hi = setup.ends_mm
    radii = [r for _, r in setup.pipe.profile]
    print(f"{thing.id}  {recon.describe()}")
    print(f"  the tube: {hi - lo:g} mm, bore radius {min(radii) * 1000:.1f} to {max(radii) * 1000:.1f} mm; holes from the mouth: "
          + ", ".join(f"{name} at {x * 1000:.1f} mm (radius {r * 1000:.1f})" for name, (x, r, _) in zip(setup.names, setup.pipe.holes)))
    print("  one value was taken for everything that is not known (python3 -m bonepipe unknowns); spread gives the range\n")
    print("  the plain fingerings: all closed, then opened one by one from the far end   (x closed, o open, from the mouth)")
    print(f"  {'fingering':<10} {'lowest':>8}     {'nearest':<8} {'':>5}   {'step':>8}   nearest ratio")
    before = None
    plain = scale.notes(setup, scale.ladder(setup.names))
    for note in plain:
        print(note_line(note, before))
        before = note
    rest = [n for n in scale.notes(setup) if n.text not in {p.text for p in plain}]
    if rest:
        print("\n  the other fingerings")
        for note in rest:
            print(note_line(note))
    print("\n  reached by blowing harder (the resonances above the lowest), in Hz")
    for note in plain:
        print(f"  {note.text:<10} {'  '.join(f'{hz:.0f} ({scale.pitch_name(hz)})' for hz in note.above) or '-'}")


def cmd_wav(thing, words: list[str]) -> None:
    if not words or "=" in words[0]:
        die(f"wav needs a file to write\n{USAGE}")
    try:
        recon = geometry.Reconstruction(thing, **given(thing, words[1:]))
    except ValueError as exc:
        die(str(exc))
    setup = geometry.build(recon)
    rate = 48000
    out = sound.array("f")
    print(f"{thing.id}  {recon.describe()}")
    for fingering in scale.ladder(setup.names):
        voice = sound.timbre(setup, fingering, rate=rate)
        text = scale.label(setup.names, fingering)
        if voice is None:
            print(f"  {text:<10} no resonance in reach")
            continue
        print(f"  {text:<10} {voice.hz:>8.1f} Hz  {scale.pitch_name(voice.hz)}")
        out.extend(sound.render([(0.7, voice)], rate))
        out.extend(sound.array("f", bytes(4 * rate // 4)))
    sound.write_wav(words[0], out, rate)
    print(f"  {words[0]}  {len(out) / rate:.2f} s: the sound is made from the resonances (bonepipe/sound.py), not recorded")


def quantile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    at = share * (len(ordered) - 1)
    lo = int(at)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)


def cmd_spread(thing, words: list[str]) -> None:
    count, seed = 200, 1
    rest = []
    it = iter(words)
    for word in it:
        if word in ("-n", "-s"):
            value = next(it, "")
            if not value.isdigit():
                die(f"{word} takes a whole number")
            count, seed = (int(value), seed) if word == "-n" else (count, int(value))
        else:
            rest.append(word)
    fixed = given(thing, rest)
    rng = random.Random(seed)
    groups: dict[tuple, dict] = {}
    for _ in range(count):
        try:
            recon = geometry.sample(thing, rng, fixed)
        except ValueError as exc:
            die(str(exc))
        setup = geometry.build(recon)
        found = scale.notes(setup, scale.ladder(setup.names))
        group = groups.setdefault((recon["blown"], recon["far"]), {"n": 0, "closed": [], "steps": {}, "turn": {}})
        group["n"] += 1
        if found[0].hz:
            group["closed"].append(found[0].hz)
        opened = list(reversed(setup.names))    # the order the holes open in
        for turn, (name, a, b) in enumerate(zip(opened, found, found[1:])):
            group["turn"].setdefault(name, []).append(turn / len(opened))
            if a.hz and b.hz:
                group["steps"].setdefault(name, []).append(scale.cents(b.hz, a.hz))
    print(f"{thing.id}  {count} reconstructions drawn from the whole range of what is not known (seed {seed})"
          + (f", with {' '.join(rest)}" if rest else ""))
    print("  the middle value, and the range that holds 90% of them\n")
    for (blown, far), group in sorted(groups.items()):
        print(f"  blown at the {blown} end, the other end {far}   ({group['n']} of them)")
        hz = group["closed"]
        if hz:
            print(f"    all closed       {quantile(hz, 0.5):>7.0f} Hz ({scale.pitch_name(quantile(hz, 0.5))})"
                  f"   {quantile(hz, 0.05):.0f} to {quantile(hz, 0.95):.0f} Hz"
                  f" ({scale.cents(quantile(hz, 0.95), quantile(hz, 0.05)):.0f} c apart)")
        for name, values in sorted(group["steps"].items(), key=lambda kv: sum(group["turn"][kv[0]]) / len(group["turn"][kv[0]])):
            print(f"    opening {name:<8} {quantile(values, 0.5):>+7.0f} c   {quantile(values, 0.05):+.0f} to {quantile(values, 0.95):+.0f} c"
                  f"   ({len(values)} with that hole)")
        print()
    print("  a step is the interval from the fingering before, as the holes open one by one from the far end;")
    print("  which fingering came before depends on which holes that reconstruction has")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    head, rest = argv[0], argv[1:]
    try:
        if head == "objects":
            cmd_objects()
        elif head == "check":
            wrong = [line for name in objects.object_ids() for line in objects.problems(objects.load(name))]
            print("\n".join(wrong) if wrong else f"check  {len(objects.object_ids())} object(s): every measurement has its source, page, method and confidence")
            return 1 if wrong else 0
        elif head in ("show", "bore", "unknowns", "notes", "spread", "wav"):
            if not rest:
                die(f"{head} which object? python3 -m bonepipe objects lists them")
            thing = objects.load(rest[0])
            {"show": lambda: cmd_show(thing), "bore": lambda: cmd_bore(thing, rest[1:]), "unknowns": lambda: cmd_unknowns(thing),
             "notes": lambda: cmd_notes(thing, rest[1:]), "spread": lambda: cmd_spread(thing, rest[1:]),
             "wav": lambda: cmd_wav(thing, rest[1:])}[head]()
        else:
            die(f"no command {head!r}\n{USAGE}")
    except objects.DataError as exc:
        die(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
