"""Measure the marrow cavity of the Divje babe I femur from the CT slices printed in the 2005 paper.

Turk, Pflaum and Pekarovič (Arheološki vestnik 56, 2005, 9-36) print 109 transverse slices of
their 16-slice CT as black silhouettes at 1:1, each with its slice number: Fig. 3, 4, 5, 8, 9
and 11. This reads those figures out of the paper's PDF with poppler's pdfimages, finds each
silhouette and measures it: the cavity where the ring of bone is closed, and the widths that can
still be read where a hole or a fracture has opened it.

Nothing of the paper is kept in this repository. Download the PDF yourself (open access,
https://ojs.zrc-sazu.si/av/article/view/8291) and run

    python3 -m bonepipe.tools.ct_slices PAPER.pdf            a table to look at
    python3 -m bonepipe.tools.ct_slices PAPER.pdf --json     the same as JSON

The figures are 300 pixels to the inch at 1:1, so a pixel is 25.4 / 300 mm. The paper says the
slices overlap by 0.25 mm at a collimation of 0.75 mm, which makes them 0.5 mm apart; slices 58
to 285 then cover 113.5 mm, against a bone 113.6 mm long by calipers.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

MM_PER_PIXEL = 25.4 / 300
FIRST_SLICE, SLICE_MM = 58, 0.5

# pdf page -> (rows, columns, the slice numbers as printed, in reading order)
FIGURES = {
    5: (5, 4, [85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107]),          # Fig. 3
    6: (4, 4, list(range(108, 124))),                                                                             # Fig. 4
    8: (5, 4, [127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 140, 146, 147, 148, 149, 150, 151, 152, 153]),  # Fig. 5
    11: (5, 4, [154, 155, 156, 157, 158, 160, 162, 164, 166, 168, 170, 172, 174, 176, 178, 180, 182, 184, 186, 188]),  # Fig. 8
    12: (6, 2, list(range(191, 203))),                                                                            # Fig. 9
    14: (5, 4, [203, 204, 205, 206, 207, 208, 209, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223, 260]),  # Fig. 11
}


def read_image(path: Path) -> tuple[int, int, bytes]:
    """A grey image out of one of pdfimages' files: one byte a pixel (of a colour file, the red)."""
    data = path.read_bytes()
    if data[:2] not in (b"P5", b"P6"):
        raise ValueError(f"{path.name} is not an image as pdfimages writes them")
    fields, pos = [], 2
    while len(fields) < 3:  # width, height, the largest value; comments start with #
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(int(data[pos:end]))
        pos = end
    width, height, _ = fields
    if data[:2] == b"P6":
        return width, height, data[pos + 1:pos + 1 + 3 * width * height:3]
    return width, height, data[pos + 1:pos + 1 + width * height]


def figure_image(pdf: Path, page: int, folder: Path) -> tuple[int, int, bytes]:
    """The largest image on a page of the PDF: the figure."""
    prefix = folder / f"p{page}"
    subprocess.run(["pdfimages", "-f", str(page), "-l", str(page), str(pdf), str(prefix)], check=True)
    files = sorted(folder.glob(f"p{page}-*.p[gp]m"), key=lambda f: f.stat().st_size)
    if not files:
        raise ValueError(f"no image on page {page}: is this the 2005 paper?")
    return read_image(files[-1])


def lines(dark_share: list[float]) -> list[int]:
    """Where the grid's lines are: the middle of each run of rows (or columns) that are nearly all dark."""
    out, start = [], None
    for i, share in enumerate(dark_share + [0.0]):
        if share > 0.85 and start is None:   # a row through four flat-topped silhouettes is 60% dark, a line all of it
            start = i
        elif share <= 0.85 and start is not None:
            out.append((start + i - 1) // 2)
            start = None
    return out


def cells(width: int, height: int, pixels: bytes, rows: int, cols: int) -> list[tuple[int, int, int, int]]:
    """The boxes of the figure's grid, in reading order, a few pixels inside the lines."""
    across = lines([sum(1 for v in pixels[y * width:(y + 1) * width] if v < 128) / width for y in range(height)])
    down = lines([sum(1 for v in pixels[x::width] if v < 128) / height for x in range(width)])
    if len(across) == rows:      # a figure whose first or last line fell off the edge of the image
        across = ([0] + across) if across[0] > height // (2 * rows) else (across + [height - 1])
    if len(down) == cols:
        down = ([0] + down) if down[0] > width // (2 * cols) else (down + [width - 1])
    if len(across) != rows + 1 or len(down) != cols + 1:
        raise ValueError(f"expected a grid of {rows} by {cols}, found {len(across) - 1} by {len(down) - 1}")
    pad = 5
    return [(down[c] + pad, across[r] + pad, down[c + 1] - pad, across[r + 1] - pad) for r in range(rows) for c in range(cols)]


def runs(values) -> list[tuple[bool, int, int]]:
    """A row or column as runs: (dark, first index, length)."""
    out, start = [], 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            out.append((bool(values[start]), start, i - start))
            start = i
    return out


def gap(values, least: int = 0) -> int | None:
    """The widest stretch of white between two stretches of dark, or None; stretches of `least`
    pixels or fewer are nicks in an edge, not openings."""
    found = [length for (dark, _, length), before, after in zip(runs(values)[1:-1], runs(values), runs(values)[2:])
             if not dark and before[0] and after[0] and length > least]
    return max(found) if found else None


def measure(width: int, pixels: bytes, box: tuple[int, int, int, int]) -> dict:
    """One silhouette: the bone is dark, the slice number in the corner is left out."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    dark = [bytearray(1 if v < 128 else 0 for v in pixels[(y0 + y) * width + x0:(y0 + y) * width + x1]) for y in range(h)]
    # the pieces of bone: connected dark pixels; the slice number sits in the top fifth and is small
    seen = [bytearray(w) for _ in range(h)]
    bone = [bytearray(w) for _ in range(h)]
    pieces = 0
    for sy in range(h):
        for sx in range(w):
            if not dark[sy][sx] or seen[sy][sx]:
                continue
            stack, found = [(sx, sy)], []
            seen[sy][sx] = 1
            while stack:
                x, y = stack.pop()
                found.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and dark[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = 1
                        stack.append((nx, ny))
            if len(found) < 400 or max(y for _, y in found) < h * 0.22:
                continue
            pieces += 1
            for x, y in found:
                bone[y][x] = 1
    ys = [y for y in range(h) if any(bone[y])]
    xs = [x for x in range(w) if any(bone[y][x] for y in ys)]
    top, bottom, left, right = ys[0], ys[-1], xs[0], xs[-1]
    # what is outside: white reached from the edge of the box
    outside = [bytearray(w) for _ in range(h)]
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    for x, y in stack:
        outside[y][x] = 1
    while stack:
        x, y = stack.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not bone[ny][nx] and not outside[ny][nx]:
                outside[ny][nx] = 1
                stack.append((nx, ny))
    inside = [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1) if not bone[y][x] and not outside[y][x]]
    mm = MM_PER_PIXEL
    out = {"pieces": pieces, "closed": len(inside) > 400,
           "outer_lm": round((right - left + 1) * mm, 2), "outer_ap": round((bottom - top + 1) * mm, 2),
           "bone_area": round(sum(sum(row) for row in bone) * mm * mm, 1)}
    middle_row = bone[(top + bottom) // 2]
    middle_col = bytearray(bone[y][(left + right) // 2] for y in range(h))
    across, down = gap(middle_row), gap(middle_col)
    out["inner_lm_mid"] = round(across * mm, 2) if across else None      # between the side walls, half way up
    out["inner_ap_mid"] = round(down * mm, 2) if down else None          # between front and back, half way across
    walls = [length for is_dark, _, length in runs(middle_row) if is_dark]
    out["wall_sides"] = [round(walls[0] * mm, 2), round(walls[-1] * mm, 2)] if len(walls) >= 2 else None
    walls = [length for is_dark, _, length in runs(middle_col) if is_dark]
    out["wall_post"] = round(walls[0] * mm, 2) if down else None         # posterior is up in the figures
    out["wall_ant"] = round(walls[-1] * mm, 2) if down else None
    third, nick = max(1, (bottom - top) // 3), round(0.8 / mm)
    # an opening above (a hole, a break) or below: the narrowest place between the two arms of bone there
    cx, cy = (left + right) // 2, (top + bottom) // 2
    for key, rows, ray in (("gap_post", range(top, top + third), range(top, cy)),
                           ("gap_ant", range(bottom - third, bottom + 1), range(cy, bottom + 1))):
        arms = [g for g in (gap(bone[y], nick) for y in rows) if g]
        open_there = not out["closed"] and not any(bone[y][cx] for y in ray)   # no bone between the middle and that side
        out[key] = round(min(arms) * mm, 2) if arms and open_there else None
    if out["closed"]:
        ix, iy = [x for x, _ in inside], [y for _, y in inside]
        out["cavity_area"] = round(len(inside) * mm * mm, 1)
        out["cavity_lm"] = round((max(ix) - min(ix) + 1) * mm, 2)
        out["cavity_ap"] = round((max(iy) - min(iy) + 1) * mm, 2)
    return out


def slices(pdf: Path) -> list[dict]:
    out = []
    with tempfile.TemporaryDirectory() as folder:
        for page, (rows, cols, numbers) in FIGURES.items():
            width, height, pixels = figure_image(pdf, page, Path(folder))
            for number, box in zip(numbers, cells(width, height, pixels, rows, cols)):
                out.append({"slice": number, "at_mm": (number - FIRST_SLICE) * SLICE_MM, **measure(width, pixels, box)})
    return sorted(out, key=lambda item: item["slice"])


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    found = slices(Path(argv[0]))
    if "--json" in argv:
        print(json.dumps(found, indent=1))
        return 0
    print("slice  at mm  pieces closed  outer LMxAP    cavity mm2  LMxAP        mid LM   mid AP  walls sides     post  ant   gap post  ant")
    for s in found:
        def show(key, width=6):
            value = s.get(key)
            return f"{value:>{width}}" if value is not None else " " * (width - 1) + "-"
        sides = "/".join(f"{v:.1f}" for v in s["wall_sides"]) if s["wall_sides"] else "-"
        print(f"{s['slice']:>5} {s['at_mm']:>6.1f} {s['pieces']:>6} {'yes' if s['closed'] else 'no':>6}  "
              f"{s['outer_lm']:>5.1f}x{s['outer_ap']:<5.1f}  {show('cavity_area', 9)}  {show('cavity_lm', 5)}x{show('cavity_ap', 5).strip():<5}"
              f" {show('inner_lm_mid', 7)} {show('inner_ap_mid', 7)}  {sides:>11} {show('wall_post', 6)} {show('wall_ant', 5)}"
              f"  {show('gap_post', 7)} {show('gap_ant', 5)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
