"""From what was measured on an object, and one reconstruction of it, the pipe that can sound.

The object as it lies in the museum is broken at both ends, and two of its holes are half gone:
it cannot sound as it is. A reconstruction says what is restored and how it is blown, one value
for everything that is not known. Every such value is a parameter here, with the range it may
plausibly take and where that range comes from, so that results can be given over the whole
range instead of for one choice.

Places along the bone are in mm from the proximal end of the CT scan (slice 58), as the slice
measurements have them.
"""
from __future__ import annotations

import math
import random

from .acoustics import Mouth, Pipe
from .objects import Thing

# name: (default, least, most, unit, what it is and where the range comes from)
NUMBERS = {
    "edge_mm": (0.7, 0.4, 1.0, "mm", "how much wider than the bone the printed CT silhouettes are, on each surface: 0.5 by the"
                                     " shaft's caliper widths, 0.4 to 1.0 by the source's own wall and hole values"),
    "prox_extra_mm": (0.0, 0.0, None, "mm", "how much further the tube went at the proximal end than what is preserved; both"
                                            " ends together no further than the whole shaft is estimated to have measured"),
    "dist_extra_mm": (0.0, 0.0, None, "mm", "the same at the distal end"),
    "flare": (0.5, 0.0, 1.0, "", "the bore past the last place it was measured: 0 no wider than there, 1 widening on as it was"),
    "mouth_open": (0.15, 0.01, 0.6, "", "the share of the blown end the lips leave open: about 0.01 to 0.02 in the calculation of"
                                        " Dimkaroski's playing (horusitzky2014), more for an end blown like a notched flute"),
    "mouth_len_mm": (3.0, 1.5, 6.0, "mm", "how far the air moves through that opening: the edge and the lip; 3.1 in horusitzky2014"),
    "hole_size": (0.5, 0.0, 1.0, "", "the complete holes between their caliper diameters (0) and their CT diameters (1)"),
    "hole3_mm": (None, None, None, "mm", "the diameter hole3 had: no less than what is left of it, no more than the complete holes"),
    "hole5_mm": (None, None, None, "mm", "the diameter hole5 had, on the same grounds"),
    "celsius": (25.0, 15.0, 35.0, "C", "the air in the tube: between a cold cave and breath"),
}
CHOICES = {
    "blown": ("proximal", "distal"),      # which end is blown: dimkaroski2014 the proximal, kunej1997 the distal
    "far": ("open", "closed"),            # the other end: open, or closed by a hand
    "hole3": (True, False),               # whether the half hole at the proximal end was a hole to finger
    "hole5": (True, False),               # whether the half hole on the anterior side was one
}
FILL = 0.77           # the cavity's area over its width times its depth, where both were measured (0.65 .. 0.80)
CLEAN_POST, CLEAN_ANT = 3.0, 4.0   # a closed slice with walls thinner than this (in the silhouette) runs through a hole's edge


class Reconstruction:
    """One value for everything that is not known about an object."""

    def __init__(self, thing: Thing, **given):
        self.thing = thing
        self.values = defaults(thing)
        for key, value in given.items():
            self.set(key, value)

    def set(self, key: str, value) -> None:
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ValueError(f"{key} is one of {', '.join(str(c) for c in CHOICES[key])}, not {value!r}")
        elif key in NUMBERS:
            lo, hi = limits(self.thing)[key]
            if not lo <= value <= hi:
                raise ValueError(f"{key} goes from {lo:g} to {hi:g}, not {value:g}")
        else:
            raise ValueError(f"nothing called {key!r}: {', '.join([*NUMBERS, *CHOICES])}")
        self.values[key] = value
        room = whole_room(self.thing)
        if self.values["prox_extra_mm"] + self.values["dist_extra_mm"] > room + 1e-9:
            raise ValueError(f"the two ends together can have gone {room:g} mm further at most: the whole shaft is estimated at"
                             f" {self.thing.value('shaft.length.whole'):g} mm and {self.thing.value('length'):g} mm are preserved")

    def __getitem__(self, key: str):
        return self.values[key]

    def describe(self) -> str:
        v = self.values
        extra = [f"{name} +{v[key]:g} mm" for name, key in (("proximal end", "prox_extra_mm"), ("distal end", "dist_extra_mm")) if v[key]]
        holes = ["hole1", "hole2"] + [h for h in ("hole3", "hole5") if v[h]]
        return (f"blown at the {v['blown']} end, {v['mouth_open'] * 100:g}% of it open; the other end {v['far']}; "
                f"{', '.join(sorted(holes))}" + (f"; {', '.join(extra)}" if extra else "") + f"; {v['celsius']:g} C")


def whole_room(thing: Thing) -> float:
    return thing.value("shaft.length.whole") - thing.value("length")


def limits(thing: Thing) -> dict[str, tuple[float, float]]:
    """The range of every number, with the ones that follow from the object's measurements worked out."""
    largest = max(thing.span("hole1.pd")[1], thing.span("hole2.pd")[1])
    out = {key: (lo, hi) for key, (_, lo, hi, _, _) in NUMBERS.items()}
    out["prox_extra_mm"] = out["dist_extra_mm"] = (0.0, whole_room(thing))
    out["hole3_mm"] = (thing.span("hole3.preserved")[1], largest)
    out["hole5_mm"] = (thing.span("hole5.preserved")[1], largest)
    return out


def defaults(thing: Thing) -> dict:
    out = {key: default for key, (default, *_rest) in NUMBERS.items()}
    lim = limits(thing)
    for key in ("hole3_mm", "hole5_mm"):
        out[key] = round(sum(lim[key]) / 2, 2)
    out.update(blown="proximal", far="open", hole3=True, hole5=False)
    return out


def sample(thing: Thing, rng: random.Random, fixed: dict | None = None) -> Reconstruction:
    """A reconstruction drawn from the whole range of everything that is not fixed."""
    fixed = fixed or {}
    lim = limits(thing)
    values = {}
    for key in NUMBERS:
        lo, hi = lim[key]
        if key == "mouth_open":   # over a range this wide, evenly in its logarithm
            values[key] = math.exp(rng.uniform(math.log(lo), math.log(hi)))
        else:
            values[key] = rng.uniform(lo, hi)
    room = whole_room(thing)
    total = rng.uniform(0.0, room)                 # how much is missing in all, then how it is shared
    share = rng.random()
    values["prox_extra_mm"], values["dist_extra_mm"] = total * share, total * (1 - share)
    for key, options in CHOICES.items():
        values[key] = rng.choice(options)
    values.update(fixed)
    if "prox_extra_mm" in fixed or "dist_extra_mm" in fixed:   # a fixed end leaves the other what room there is
        for key, other in (("prox_extra_mm", "dist_extra_mm"), ("dist_extra_mm", "prox_extra_mm")):
            if key in fixed and other not in fixed:
                values[other] = rng.uniform(0.0, max(0.0, room - fixed[key]))
    made = Reconstruction(thing)
    for key in ("prox_extra_mm", "dist_extra_mm"):
        made.values[key] = 0.0
    for key, value in values.items():
        made.set(key, value)
    return made


# ---- the bore

def stations(thing: Thing, edge_mm: float) -> list[dict]:
    """The marrow cavity along the bone: {at_mm, area_mm2, basis} from the slice measurements.

    Where the bone is a closed ring away from the holes, the cavity was measured (basis
    "measured"). Where a hole or a break has opened it, the width between the side walls still
    was, and the depth is taken as the silhouette's height less the two walls as they are in the
    nearest closed slice (basis "estimated"). Slices through a hole's edge are left out, and the
    bore runs straight between its neighbours there. Every surface is then moved by edge_mm.
    """
    rows = thing.slices()["slices"]
    clean = [r for r in rows if r["closed"] and r["wall_post"] >= CLEAN_POST and r["wall_ant"] >= CLEAN_ANT]
    first, last = clean[0]["at_mm"], clean[-1]["at_mm"]
    out = []
    for r in rows:
        if r in clean:
            lm, ap, area, basis = r["cavity_lm"], r["cavity_ap"], r["cavity_area"], "measured"
        elif first < r["at_mm"] < last or r["inner_lm_mid"] is None:
            continue    # through a hole in the closed stretch: the neighbours carry the bore across
        else:
            near = min(clean, key=lambda c: abs(c["at_mm"] - r["at_mm"]))
            lm = r["inner_lm_mid"]
            ap = r["outer_ap"] - near["wall_post"] - near["wall_ant"]
            area, basis = FILL * lm * ap, "estimated"
        grown = area * (lm + 2 * edge_mm) * (ap + 2 * edge_mm) / (lm * ap)
        out.append({"at_mm": r["at_mm"], "area_mm2": grown, "radius_mm": math.sqrt(grown / math.pi), "basis": basis,
                    "slice": r["slice"]})
    return out


def openings(thing: Thing) -> dict[str, tuple[float, float]]:
    """Where the slices show each hole: (first, last) place the wall is open there, in mm."""
    rows = thing.slices()["slices"]
    step = thing.slices()["slice_mm"]

    def stretch(key: str, lo: float, hi: float) -> tuple[float, float]:
        found = [r["at_mm"] for r in rows if r[key] and lo <= r["at_mm"] <= hi]
        return found[0] - step / 2, found[-1] + step / 2

    closed = [r["at_mm"] for r in rows if r["closed"]]
    middle = (closed[0] + closed[-1]) / 2
    return {"hole1": stretch("gap_post", closed[0], middle),
            "hole2": stretch("gap_post", middle, closed[-1] + 20),
            "hole3": (closed[0] - 20, closed[0] - step / 2),          # only its distal edge is left: where the ring closes
            "hole5": (closed[-1] + step / 2, closed[-1] + 20)}        # only its proximal edge: where the ring opens


class Setup:
    """A reconstruction ready to sound: the pipe, its mouth, its far end and its holes by name."""

    def __init__(self, pipe: Pipe, mouth: Mouth, far: str, names: list[str], recon: Reconstruction, ends_mm: tuple[float, float]):
        self.pipe, self.mouth, self.far, self.names, self.recon, self.ends_mm = pipe, mouth, far, names, recon, ends_mm

    def opened(self, fingering: dict[str, bool]) -> list[bool]:
        return [bool(fingering.get(name)) for name in self.names]

    def resonances(self, fingering: dict[str, bool], **more):
        return self.pipe.resonances(self.opened(fingering), self.far, self.mouth, **more)


def build(recon: Reconstruction) -> Setup:
    thing, v = recon.thing, recon.values
    edge = v["edge_mm"]
    found = stations(thing, edge)
    lo_mm, hi_mm = -v["prox_extra_mm"], thing.value("length") + v["dist_extra_mm"]

    def slope(points: list[dict]) -> float:
        """How fast the radius changes along these stations (least squares), mm per mm."""
        n = len(points)
        mx, mr = sum(p["at_mm"] for p in points) / n, sum(p["radius_mm"] for p in points) / n
        den = sum((p["at_mm"] - mx) ** 2 for p in points)
        return sum((p["at_mm"] - mx) * (p["radius_mm"] - mr) for p in points) / den if den else 0.0

    head = [p for p in found if p["at_mm"] <= found[0]["at_mm"] + 10]
    tail = [p for p in found if p["at_mm"] >= found[-1]["at_mm"] - 25]   # the last stations are far apart: take enough of them
    points = [(p["at_mm"], p["radius_mm"]) for p in found if lo_mm <= p["at_mm"] <= hi_mm]
    if lo_mm < found[0]["at_mm"]:     # past the first place measured: widening on towards the end, by flare
        widen = max(0.0, -slope(head)) * v["flare"]
        points.insert(0, (lo_mm, found[0]["radius_mm"] + widen * (found[0]["at_mm"] - lo_mm)))
    if hi_mm > found[-1]["at_mm"]:
        widen = max(0.0, slope(tail)) * v["flare"]
        points.append((hi_mm, found[-1]["radius_mm"] + widen * (hi_mm - found[-1]["at_mm"])))

    where = openings(thing)
    u = v["hole_size"]

    def between(what: str) -> float:
        return thing.value(what, "calipers") * (1 - u) + thing.value(what, "ct") * u

    holes = {}
    for name in ("hole1", "hole2"):
        lm, pd = between(f"{name}.lm"), between(f"{name}.pd")
        wall = thing.span(f"wall.at_{name}")
        holes[name] = (sum(where[name]) / 2, math.sqrt(lm * pd) / 2, sum(wall) / 2)
    if v["hole3"]:   # its distal edge is where the ring closes; the silhouette's edge sits edge_mm inside the hole
        d = v["hole3_mm"]
        holes["hole3"] = (where["hole3"][1] + edge - d / 2, d / 2, sum(thing.span("wall.at_hole3")) / 2)
    if v["hole5"]:
        d = v["hole5_mm"]
        holes["hole5"] = (where["hole5"][0] - edge + d / 2, d / 2, sum(thing.span("wall.at_hole5")) / 2)

    turn = v["blown"] == "distal"      # the acoustics counts from the blown end

    def place(at_mm: float) -> float:
        return ((hi_mm - at_mm) if turn else (at_mm - lo_mm)) / 1000

    profile = sorted((place(x), r / 1000) for x, r in points)
    names = sorted(holes, key=lambda n: place(holes[n][0]))
    pipe = Pipe(profile, [(place(holes[n][0]), holes[n][1] / 1000, holes[n][2] / 1000) for n in names], v["celsius"])
    end_radius = profile[0][1]
    mouth = Mouth(end_radius * math.sqrt(v["mouth_open"]), v["mouth_len_mm"] / 1000)
    return Setup(pipe, mouth, v["far"], names, recon, (lo_mm, hi_mm))
