"""The time map of a video track: which moment of the picture is shown at each moment of the project.

Pure arithmetic, no files. Everything works in cells of FRAME_MS (25 ms), the grid of analysis.py, and
a map is a list with one source position (in cells, a float) for every project cell boundary. What
a track stores are warp points, [ms after the track's position, ms in the source], with straight
lines between them.

Fitting a picture to music has two stages.

1. The motion budget, always. What the eye sees at a moment is the picture's own rate of change times
   how fast the map runs through it, P = motion(source) x slope. P should follow the music's level
   (kept above a floor, so silence does not stop the picture). Match the running total of one to the
   running total of the other and the map falls out: the source position whose motion so far is the
   same share of all its motion as the music so far is of all the music. `depth` mixes that with a
   plain even stretch (0 is the even stretch). The slopes are smoothed, then pressed into the allowed
   range [1/slow, fast] by the least change that still adds up to the whole stretch (water-filling), so
   the map is monotonic, covers the range, and never slows down more than `slow` times. With a
   picture whose motion is flat this is the music-level map; with music that is flat it evens out
   the picture's pace.
2. The hits. A music hit (a strong onset, and a bar line when there is a tempo) is given the picture's
   own fastest moment near it, if there is one within about 0.75 s: an anchor (hit, source position of
   that moment). The anchors that can all be kept, forward and within the slope limits, are chosen for
   the largest total strength, and between them the stage 1 slopes are pressed to fit. The result is
   kept only when more hits come to have a fast moment within 0.1 s and the level match does not get
   worse.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .analysis import FRAME_MS

SMOOTH_CELLS = 40       # slopes are averaged over a second before they are pressed into range
MATCH_CELLS = 20        # the level match is read over half a second
FLOOR = 0.1             # the music's weight in silence, next to 0..1 for its level
ROOM = 1.25             # looping or bouncing a short picture: slow it by 80% of what is allowed, leaving room to move
HIT = 0.3               # onset that counts as a hit, as in video.HIT
NEAR_CELLS = 30         # a hit looks this far for a fast moment of the picture (0.75 s)
CLOSE_CELLS = 4         # a hit is "met" by a fast moment this close (0.1 s)
BAR_WEIGHT = 0.25       # a bar line is worth this next to a hit's strength
POINT_TOLERANCE_MS = 40 # stored points leave the map no further than this (one frame at 25 fps)
POINT_MIN_MS = 250      # and are no closer together than this


@dataclass
class Fit:
    src: list[float]                 # source position (cells) at every project cell boundary 0 .. covered
    used: float                      # cells of source the map runs through (may be more than the file, with a tail)
    covered: int                     # project cells the map covers
    wanted: int                      # project cells asked for
    hits: int = 0                    # music hits there are
    hits_before: int = 0             # met by a fast moment of the picture with an even stretch
    hits_after: int = 0              # and with the map
    anchored: int = 0                # hits the map was bent to meet
    match_before: float = 0.0        # level match with an even stretch (-1 .. 1)
    match_after: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def gap(self) -> int:
        return self.wanted - self.covered


# ---- small tools

def box(values: list[float], window: int) -> list[float]:
    """A moving average over `window` cells, centred, shorter at the ends."""
    n = len(values)
    if n == 0 or window <= 1:
        return list(values)
    sums = [0.0]
    for v in values:
        sums.append(sums[-1] + v)
    half = window // 2
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((sums[b] - sums[a]) / (b - a))
    return out


def water_fill(desired: list[float], lo: float, hi: float, total: float) -> list[float]:
    """The slopes nearest `desired` (least squares) that stay within [lo, hi] and add up to `total`.
    `total` is held to what the bounds allow."""
    n = len(desired)
    if n == 0:
        return []
    total = min(max(total, lo * n), hi * n)
    a, b = lo - max(desired), hi - min(desired)  # shifting by a puts every slope at lo, by b at hi
    for _ in range(80):
        mid = (a + b) / 2
        if sum(min(hi, max(lo, d + mid)) for d in desired) < total:
            a = mid
        else:
            b = mid
    shift = (a + b) / 2
    out = [min(hi, max(lo, d + shift)) for d in desired]
    free = [i for i, v in enumerate(out) if lo < v < hi]  # what the bisection left over goes to the free ones
    rest = total - sum(out)
    if free and abs(rest) > 1e-12:
        for i in free:
            out[i] += rest / len(free)
    return out


def running(values: list[float]) -> list[float]:
    total, out = 0.0, [0.0]
    for v in values:
        total += v
        out.append(total)
    return out


def pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = sum(a[:n]) / n, sum(b[:n]) / n
    va = sum((x - ma) ** 2 for x in a[:n])
    vb = sum((y - mb) ** 2 for y in b[:n])
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def peaks(values: list[float], radius: int, floor: float) -> list[int]:
    """Cells that are the highest within `radius` cells either side and reach `floor`."""
    out = []
    for i, v in enumerate(values):
        if v < floor:
            continue
        window = values[max(0, i - radius):i + radius + 1]
        if v >= max(window) and (not out or i - out[-1] > radius // 2):
            out.append(i)
    return out


# ---- the source

def extend(motion: list[float], cells: float, tail: str) -> list[float]:
    """The motion curve as far as the map runs: the file's own, then its loop or its bounce when it
    is shorter than that."""
    need = math.ceil(cells)
    if need <= len(motion) or not tail or not motion:
        return list(motion[:need])
    out: list[float] = []
    k = 0
    while len(out) < need:
        out.extend(motion if tail == "loop" or k % 2 == 0 else motion[::-1])
        k += 1
    return out[:need]


def fold(x: float, length: float, tail: str) -> float:
    """Where in the file of `length` the source position x is: itself, or the loop's or the bounce's."""
    if not tail or length <= 0 or 0 <= x <= length:
        return x
    if tail == "loop":
        return x % length
    y = x % (2 * length)
    return y if y <= length else 2 * length - y


# ---- stage 1

def matched(motion: list[float], level: list[float], used: float, depth: float) -> list[float]:
    """Source position at every project cell boundary: the music's running share of its total set
    against the picture's running share of its motion, mixed with an even stretch by depth."""
    t = len(level)
    need = math.ceil(used)
    mean = sum(motion[:need]) / max(1, need)
    cells = [m + 0.05 * mean for m in motion[:need]]
    cv = [0.0]
    for k, m in enumerate(cells):
        cv.append(cv[-1] + m * min(1.0, used - k))
    music = running([FLOOR + v for v in level])
    out = []
    for i in range(t + 1):
        target = music[i] / music[-1] * cv[-1]
        k = min(len(cv) - 2, max(0, bisect.bisect_right(cv, target) - 1))
        step = cv[k + 1] - cv[k]
        x = k + ((target - cv[k]) / step if step > 0 else 0.0)
        out.append((1 - depth) * used * i / t + depth * min(used, x))
    out[-1] = used
    return out


def press(star: list[float], lo: float, hi: float, total: float) -> list[float]:
    """A map from a wished-for one: its slopes smoothed and pressed into [lo, hi], adding up to total."""
    slopes = box([b - a for a, b in zip(star, star[1:])], SMOOTH_CELLS)
    return running(water_fill(slopes, lo, hi, total))


# ---- measuring

def seen(motion: list[float], src: list[float]) -> list[float]:
    """P: the picture's change per second of project time, as the eye gets it, for every project cell."""
    out = []
    for a, b in zip(src, src[1:]):
        k = min(len(motion) - 1, max(0, int((a + b) / 2)))
        out.append(motion[k] * (b - a))
    return out


def match(speed: list[float], level: list[float]) -> float:
    return pearson(box(speed, MATCH_CELLS), box(level, MATCH_CELLS))


def hits_in(onset: list[float]) -> list[int]:
    return peaks(onset, 4, HIT)


def fast_moments(speed: list[float]) -> list[int]:
    smooth = box(speed, 3)
    if not smooth:
        return []
    mean = sum(smooth) / len(smooth)
    spread = math.sqrt(sum((v - mean) ** 2 for v in smooth) / len(smooth))
    return peaks(smooth, 6, mean + 0.5 * spread if spread > 0 else math.inf)


def met(hits: list[int], moments: list[int]) -> int:
    return sum(1 for h in hits if any(abs(h - m) <= CLOSE_CELLS for m in moments))


# ---- stage 2

def anchors(candidates: list[tuple[int, float]], moments: list[int], src: list[float]) -> list[tuple[int, float, float]]:
    """(project cell, source position, strength): each music candidate with the picture's fast moment
    nearest to it within NEAR_CELLS, whose source position is what should be shown there."""
    out = []
    for cell, weight in candidates:
        near = [m for m in moments if abs(m - cell) <= NEAR_CELLS]
        if near:
            m = min(near, key=lambda m: abs(m - cell))
            out.append((cell, src[m], weight))
    return out


def chain(items: list[tuple[int, float, float]], total_cells: int, used: float, lo: float, hi: float):
    """The anchors that can all be kept (forward in both, within the slope limits between neighbours,
    and from the start and to the end of the stretch) for the greatest total strength."""
    items = sorted({h: (h, s, w) for h, s, w in sorted(items, key=lambda a: a[2])}.values())
    nodes = [(0, 0.0, 0.0), *[a for a in items if 0 < a[0] < total_cells], (total_cells, used, 0.0)]
    best, back = [0.0] + [-math.inf] * (len(nodes) - 1), [-1] * len(nodes)
    for j in range(1, len(nodes)):
        hj, sj, wj = nodes[j]
        for i in range(j):
            if best[i] == -math.inf:
                continue
            dh, ds = hj - nodes[i][0], sj - nodes[i][1]
            if dh > 0 and ds > 0 and lo * dh - 1e-9 <= ds <= hi * dh + 1e-9 and best[i] + wj > best[j]:
                best[j], back[j] = best[i] + wj, i
    path, j = [], len(nodes) - 1
    while j > 0:
        path.append(nodes[j])
        j = back[j]
    return path[::-1][:-1]  # the end node is not an anchor


def bend(star: list[float], kept: list[tuple[int, float, float]], lo: float, hi: float) -> list[float]:
    """The map through the kept anchors, each stretch between them with the stage 1 slopes pressed to fit."""
    slopes = box([b - a for a, b in zip(star, star[1:])], SMOOTH_CELLS)
    marks = [(0, 0.0), *[(h, s) for h, s, _ in kept], (len(slopes), star[-1])]
    out = [0.0]
    for (h0, s0), (h1, s1) in zip(marks, marks[1:]):
        for v in water_fill(slopes[h0:h1], lo, hi, s1 - s0):
            out.append(out[-1] + v)
    return out


# ---- the whole fit

def fit(motion: list[float], level: list[float], onset: list[float], source_cells: float, *, slow: float,
        fast: float, depth: float, tail: str = "", bars: list[int] | None = None) -> Fit:
    """A map from the wanted stretch (len(level) cells of music) to the picture (source_cells of it,
    motion one rate per cell)."""
    t = len(level)
    lo, hi = 1.0 / slow, fast
    notes: list[str] = []
    if t < 2:
        raise ValueError("the stretch to fill is too short")
    if source_cells >= lo * t:
        used = min(source_cells, hi * t)
        if used < source_cells:
            notes.append("long")
        covered = t
    elif tail:
        used, covered = min(hi * t, max(source_cells, lo * t * ROOM)), t
    else:  # too short even slowed down as far as allowed: the whole of it, at that slowest pace
        covered = int(source_cells / lo)
        src = [i * lo for i in range(covered + 1)]
        return Fit(src, source_cells, covered, t, notes=["short"])
    curve = extend(list(motion), used, tail)
    star = matched(curve, level, used, depth)
    src = press(star, lo, hi, used)
    speed_even = seen(curve, [used * i / t for i in range(t + 1)])
    result = Fit(src, used, covered, t, notes=notes)
    result.match_before = match(speed_even, level)
    speed = seen(curve, src)
    result.match_after = match(speed, level)
    music = hits_in(onset)
    result.hits = len(music)
    result.hits_before = met(music, fast_moments(speed_even))
    moments = fast_moments(speed)
    candidates = [(h, onset[h]) for h in music]
    for bar in bars or []:
        if 0 < bar < t and not any(abs(bar - h) <= CLOSE_CELLS for h in music):
            candidates.append((bar, BAR_WEIGHT))
    targets = [cell for cell, _ in candidates]  # what the map is judged by: hits and bar lines alike
    kept = chain(anchors(candidates, moments, src), t, used, lo, hi)
    if kept:
        bent = bend(star, kept, lo, hi)
        speed_bent = seen(curve, bent)
        moments_bent = fast_moments(speed_bent)
        match_bent = match(speed_bent, level)
        if met(targets, moments_bent) > met(targets, moments) and match_bent >= result.match_after - 0.05:
            result.src, result.anchored = bent, len(kept)
            result.match_after = match_bent
            result.hits_after = met(music, moments_bent)
            return result
    result.hits_after = met(music, moments)
    return result


# ---- points

def simplify(src: list[float], in_ms: int, keep: set[int] | None = None) -> list[list[int]]:
    """The map as warp points: [ms after the start, ms in the source], few enough to edit by hand,
    none further from the map than POINT_TOLERANCE_MS and none closer together than POINT_MIN_MS
    (the cells in `keep`, bar lines, always stay). Ends are exact."""
    n = len(src) - 1
    tolerance = POINT_TOLERANCE_MS / FRAME_MS
    min_gap = POINT_MIN_MS // FRAME_MS
    chosen = {0, n} | {k for k in (keep or set()) if 0 < k < n}

    def split(a: int, b: int) -> None:
        if b - a < 2 * min_gap:
            return
        worst, at = 0.0, -1
        for i in range(a + min_gap, b - min_gap + 1):
            err = abs(src[i] - (src[a] + (src[b] - src[a]) * (i - a) / (b - a)))
            if err > worst:
                worst, at = err, i
        if at >= 0 and worst > tolerance:
            chosen.add(at)
            split(a, at)
            split(at, b)

    marks = sorted(chosen)
    for a, b in zip(marks, marks[1:]):
        split(a, b)
    out: list[list[int]] = []
    for i in sorted(chosen):
        point = [i * FRAME_MS, in_ms + round(src[i] * FRAME_MS)]
        if out and point[1] < out[-1][1]:
            point[1] = out[-1][1]
        out.append(point)
    return out


def evaluate(points: list[list[int]], at: float) -> float:
    """The source position (ms) at `at` (ms after the start), straight between points, and on
    past the ends at the slope of the first and last stretch."""
    xs = [p[0] for p in points]
    if len(points) == 1:
        return points[0][1] + (at - xs[0])
    k = min(len(points) - 2, max(0, bisect.bisect_right(xs, at) - 1))
    (x0, s0), (x1, s1) = points[k], points[k + 1]
    return s0 + (s1 - s0) * (at - x0) / (x1 - x0)


def slowdowns(points: list[list[int]]) -> list[float]:
    """How many times slower than the file each stretch between points is shown (inf when it stands still)."""
    return [(b[0] - a[0]) / (b[1] - a[1]) if b[1] > a[1] else math.inf for a, b in zip(points, points[1:])]
