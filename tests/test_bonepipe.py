"""bonepipe: the data holds together, the acoustics agrees with what is known, the reports run.

bonepipe does not use gout, so nothing here needs ffmpeg. The reference frequencies were computed
with OpenWInD 0.12.4 (finite elements, losses on, unflanged radiation, matching volume on, 20 C)
on 2026-10-06; with openwind installed the comparison is also made live.
"""
import math
import random
import subprocess
import sys
import unittest

from helpers import REPO

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bonepipe import geometry, objects, scale  # noqa: E402
from bonepipe.acoustics import Air, cents, Mouth, Pipe  # noqa: E402
from bonepipe.tools import ct_slices  # noqa: E402

R = math.sqrt(6.5 * 5.0) / 1000                                    # a circle with the area of a 13 x 10 mm oval
HOLES = [(0.042, 0.0044, 0.0041), (0.077, 0.0045, 0.0041)]          # place, radius, chimney: of the size of the find's
TUBES = {"cylinder": [(0.0, R), (0.1136, R)], "flared": [(0.0, 0.0080), (0.020, R), (0.085, R), (0.1136, 0.0095)]}
OPENWIND = {  # the two lowest impedance minima at the end, in Hz, for (near hole open, far hole open)
    "cylinder": {(False, False): (1414.7, 2853.6), (False, True): (1866.7, 3310.2),
                 (True, False): (1888.3, 3241.2), (True, True): (2555.5, 3438.5)},
    "flared": {(False, False): (1644.7, 3077.9), (False, True): (2017.1, 3508.8),
               (True, False): (2129.6, 3461.8), (True, True): (2707.4, 3636.7)},
}


def bonepipe(*args: str, ok: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, "-m", "bonepipe", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    if ok != (result.returncode == 0):
        raise AssertionError(f"bonepipe {' '.join(args)} gave {result.returncode}:\n{result.stdout}{result.stderr}")
    return result


class DataTest(unittest.TestCase):
    def setUp(self):
        self.thing = objects.load("divje-babe-1")

    def test_every_number_has_a_source_that_was_read(self):
        self.assertEqual(objects.problems(self.thing), [])
        known = objects.sources()
        for m in self.thing.measurements:
            self.assertTrue(known[m["source"]]["read"], m)
        # a number from a source nobody read, or with no page, is a problem
        import copy
        bad = objects.Thing(copy.deepcopy(self.thing.data))
        bad.measurements.append({"what": "length", "value": 1, "unit": "mm", "method": "ct", "source": "tuniz2012",
                                 "page": "1", "confidence": "high"})
        bad.measurements.append({"what": "length", "value": 1, "unit": "mm", "method": "guess", "source": "turk1997",
                                 "page": "", "confidence": "high"})
        found = "\n".join(objects.problems(bad))
        self.assertIn("tuniz2012 was not read", found)
        self.assertIn("has no page", found)
        self.assertIn("method 'guess'", found)

    def test_the_measurements_are_what_the_sources_say(self):
        t = self.thing
        self.assertEqual(t.value("length"), 113.6)                                    # turk1997 p. 161
        self.assertEqual((t.value("hole1.lm", "calipers"), t.value("hole1.pd", "calipers")), (8.2, 9.7))
        self.assertEqual((t.value("hole1.lm", "ct"), t.value("hole1.pd", "ct")), (8.4, 10.5))   # turk2005 p. 31
        self.assertEqual((t.value("hole2.lm", "ct"), t.value("hole2.pd", "ct")), (9.2, 11.1))   # p. 33
        self.assertEqual(t.value("hole1-hole2.centres"), 35)
        self.assertEqual(t.span("hole3.preserved"), (6.5, 7.6))
        with self.assertRaisesRegex(objects.DataError, "say which method"):
            t.value("hole1.lm")
        with self.assertRaisesRegex(objects.DataError, "no object called 'nothing'"):
            objects.load("nothing")
        self.assertEqual(t.data["status"], "contested")

    def test_the_slices_agree_with_the_calipers(self):
        data = self.thing.slices()
        rows = data["slices"]
        self.assertEqual(len(rows), 108)
        closed = [r["slice"] for r in rows if r["closed"]]
        self.assertEqual((closed[0], closed[-1], len(closed)), (115, 195, 44))        # the only stretch that is a whole ring
        # the silhouettes are about half a millimetre wider than the bone on each surface
        self.assertAlmostEqual(min(r["outer_lm"] for r in rows) - self.thing.value("shaft.lm.narrowest"), 1.0, delta=0.15)
        self.assertAlmostEqual(min(r["outer_ap"] for r in rows) - self.thing.value("shaft.ap.narrowest"), 1.1, delta=0.15)
        where = geometry.openings(self.thing)
        self.assertEqual((where["hole1"], where["hole2"]), ((37.75, 46.25), (71.75, 80.25)))
        middle = {name: sum(where[name]) / 2 for name in ("hole1", "hole2")}
        self.assertAlmostEqual(middle["hole2"] - middle["hole1"], self.thing.value("hole1-hole2.centres"), delta=1.5)
        # the proximal edges of hole2 and hole5 are 3 to 4 mm apart in the source: here, between slice positions
        self.assertAlmostEqual(where["hole2"][0] - where["hole5"][0], 3.5, delta=1.0)
        setup = geometry.build(geometry.Reconstruction(self.thing, hole3=True))
        at = dict(zip(setup.names, (h[0] * 1000 for h in setup.pipe.holes)))
        self.assertAlmostEqual(at["hole1"] - at["hole3"], self.thing.value("hole3-hole1.centres"), delta=1.5)

    def test_the_bore_is_measured_in_the_middle_and_estimated_at_the_ends(self):
        found = geometry.stations(self.thing, 0.7)
        measured = [s for s in found if s["basis"] == "measured"]
        self.assertEqual((measured[0]["at_mm"], measured[-1]["at_mm"]), (29.0, 67.5))
        narrowest = min(found, key=lambda s: s["area_mm2"])
        self.assertTrue(55 <= narrowest["at_mm"] <= 62, narrowest)
        self.assertAlmostEqual(narrowest["radius_mm"], 5.7, delta=0.15)
        # a wider edge makes a wider bore, everywhere
        thin, wide = geometry.stations(self.thing, 0.4), geometry.stations(self.thing, 1.0)
        self.assertTrue(all(a["area_mm2"] < b["area_mm2"] for a, b in zip(thin, wide)))
        self.assertAlmostEqual(wide[40]["area_mm2"] / thin[40]["area_mm2"], 1.25, delta=0.1)

    def test_the_slice_tool_measures_a_ring_and_a_ring_with_a_gap(self):
        size, mm = 240, ct_slices.MM_PER_PIXEL

        def picture(gap: int) -> bytes:
            out = bytearray(b"\xff" * (size * size))
            for y in range(size):
                for x in range(size):
                    r = math.hypot(x - 120, y - 130)
                    if 40 <= r <= 80 and not (gap and abs(x - 120) < gap / 2 and y < 130):
                        out[y * size + x] = 0
            for y in range(8, 20):          # a slice number in the corner, which is not bone
                for x in range(8, 30):
                    out[y * size + x] = 0
            return bytes(out)

        ring = ct_slices.measure(size, picture(0), (0, 0, size, size))
        self.assertTrue(ring["closed"])
        self.assertEqual(ring["pieces"], 1)
        self.assertAlmostEqual(ring["cavity_area"], math.pi * 40 ** 2 * mm * mm, delta=0.6)
        self.assertAlmostEqual(ring["cavity_lm"], 80 * mm, delta=0.2)
        self.assertAlmostEqual(ring["outer_lm"], 161 * mm, delta=0.2)
        self.assertAlmostEqual(ring["wall_post"], 40 * mm, delta=0.2)
        cut = ct_slices.measure(size, picture(30), (0, 0, size, size))
        self.assertFalse(cut["closed"])
        self.assertAlmostEqual(cut["gap_post"], 30 * mm, delta=0.2)
        self.assertIsNone(cut["gap_ant"])
        self.assertAlmostEqual(cut["inner_lm_mid"], 80 * mm, delta=0.3)


class AcousticsTest(unittest.TestCase):
    def test_an_open_tube_a_stopped_tube_and_a_bottle(self):
        air = Air(20.0)
        tube = Pipe([(0.0, 0.006), (0.3, 0.006)], celsius=20.0)

        def speed(hz: float) -> float:
            """Sound in a tube of 6 mm radius: the walls slow it, by 1% at these frequencies."""
            thin = 0.006 * math.sqrt(2 * math.pi * hz * air.rho / air.mu)
            return air.c * (1 - (1 + air.heat) / (math.sqrt(2) * thin))

        # open at both ends, the blown end ideally so: half a wave in the length and the far end's correction
        got = tube.resonances(lo=100, hi=2000)[0].hz
        self.assertAlmostEqual(cents(got, speed(got) / (2 * (0.3 + 0.6133 * 0.006))), 0.0, delta=2.0)
        self.assertAlmostEqual(cents(got, air.c / (2 * (0.3 + 0.6133 * 0.006))), -19.0, delta=3.0)   # the walls alone: 19 cents
        # stopped at the far end: a quarter wave
        got = tube.resonances(far="closed", lo=100, hi=2000)[0].hz
        self.assertAlmostEqual(cents(got, speed(got) / (4 * 0.3)), 0.0, delta=2.0)
        # stopped, with a small mouth: a bottle (Helmholtz), far below the cavity's own notes. Without losses the
        # mouth's mass against the cavity's spring gives  k * neck * (cavity area / mouth area) = cot(k * depth)
        mouth = Mouth(0.003, 0.004)
        bottle = Pipe([(0.0, 0.010), (0.040, 0.010)], celsius=20.0)
        got = bottle.resonances(far="closed", mouth=mouth, lo=50, hi=3000)[0]
        neck = 0.004 + 0.8216 * 0.003 * (1 - 1.35 * 0.3 + 0.31 * 0.3 ** 3) + 0.8216 * 0.003
        lo, hi = 1.0, math.pi / 2 / 0.040
        for _ in range(60):
            k = (lo + hi) / 2
            lo, hi = (k, hi) if k * neck * (0.010 / 0.003) ** 2 < 1 / math.tan(k * 0.040) else (lo, k)
        lossless = k * air.c / (2 * math.pi)
        self.assertTrue(-45.0 < cents(got.hz, lossless) < 0.0, (got.hz, lossless))   # the walls of the mouth slow it a little
        self.assertLess(got.hz, air.c / (4 * 0.040) / 2)
        self.assertLess(got.q, tube.resonances(lo=100, hi=2000)[0].q)       # and a duller resonance than a pipe's

    def test_it_agrees_with_openwind_on_tubes_the_size_of_the_find(self):
        worst = 0.0
        for name, profile in TUBES.items():
            tube = Pipe(profile, HOLES, celsius=20.0)
            for opened, want in OPENWIND[name].items():
                got = tube.resonances(opened, lo=200, hi=5000)
                for mine, theirs in zip(got, want):
                    worst = max(worst, abs(cents(mine.hz, theirs)))
        self.assertLess(worst, 8.0)

    def test_it_agrees_with_openwind_when_openwind_is_here(self):
        try:
            import numpy
            from openwind import ImpedanceComputation
        except ImportError:
            self.skipTest("openwind is not installed (pip install openwind): the stored reference is used instead")
        freqs = numpy.arange(200.0, 5000.0, 2.0)
        profile = TUBES["flared"]
        bore = [[x0, x1, r0, r1, "linear"] for (x0, r0), (x1, r1) in zip(profile, profile[1:])]
        holes = [["label", "position", "radius", "chimney"]] + [[f"h{i + 1}", *hole] for i, hole in enumerate(HOLES)]
        chart = [["label", "closed", "open"], ["h1", "x", "o"], ["h2", "x", "o"]]
        tube = Pipe(profile, HOLES, celsius=20.0)
        for note, opened in (("closed", (False, False)), ("open", (True, True))):
            theirs = ImpedanceComputation(freqs, bore, holes, chart, note=note, temperature=20, losses=True,
                                          radiation_category="unflanged", matching_volume=True).antiresonance_frequencies(2)
            for mine, ref in zip(tube.resonances(opened, lo=200, hi=5000), theirs):
                self.assertLess(abs(cents(mine.hz, ref)), 8.0)

    def test_it_lands_near_the_published_calculation_of_dimkaroskis_copy(self):
        # horusitzky2014, p. 228-229: sections of radius 9.1, 6.1, 6.1 and 7.68 mm over 22, 18, 35 and 26 mm, holes of
        # radius 3.25, 4.45 and 4.425 mm with 4 mm of wall, sound at 347 m/s, a bevel of 1.9 mm at the lips. With the far
        # end closed the lips leaving 2 mm give f1 and 2.74 mm give a1; with it open, 2 mm give a2.
        sections = [(0.0, 0.0091), (0.022, 0.0091), (0.022, 0.0061), (0.075, 0.0061), (0.075, 0.00768), (0.101, 0.00768)]
        copy = Pipe(sections, [(0.022, 0.00325, 0.004), (0.040, 0.00445, 0.004), (0.075, 0.004425, 0.004)], celsius=27.0)
        self.assertAlmostEqual(copy.air.c, 347.0, delta=0.6)
        for lips, far, hz in ((0.002, "closed", 349.23), (0.00274, "closed", 440.0), (0.002, "open", 880.0)):
            got = copy.resonances((False, False, False), far=far, mouth=Mouth(lips / 2, 0.0019), lo=100, hi=3000)[0].hz
            self.assertLess(abs(cents(got, hz)), 70.0, (lips, far, got))   # another model's numbers: half a semitone is near

    def test_holes_and_warmth_raise_the_pitch(self):
        tube = Pipe(TUBES["cylinder"], HOLES, celsius=20.0)
        closed, far, near, both = (tube.resonances(o, lo=200, hi=5000)[0].hz for o in
                                   ((False, False), (False, True), (True, False), (True, True)))
        self.assertLess(closed, far)
        self.assertLess(far, both)
        self.assertLess(near, both)
        warm = Pipe(TUBES["cylinder"], HOLES, celsius=30.0).resonances((False, False), lo=200, hi=5000)[0].hz
        self.assertAlmostEqual(cents(warm, closed), 1200 * math.log2(math.sqrt(303.15 / 293.15)), delta=1.0)   # 29 cents
        lips = tube.resonances((False, False), mouth=Mouth(0.002, 0.003), lo=100, hi=5000)[0].hz
        self.assertLess(lips, closed)                                      # a smaller mouth flattens


class ReconstructionTest(unittest.TestCase):
    def setUp(self):
        self.thing = objects.load("divje-babe-1")

    def lowest(self, **given) -> float:
        setup = geometry.build(geometry.Reconstruction(self.thing, **given))
        return setup.resonances({name: False for name in setup.names})[0].hz

    def test_what_is_not_known_is_a_parameter_with_a_range(self):
        lim = geometry.limits(self.thing)
        self.assertEqual(lim["hole3_mm"], (7.6, 11.1))                # no less than is left of it, no more than the complete holes
        self.assertEqual(lim["hole5_mm"], (6.2, 11.1))
        self.assertAlmostEqual(lim["prox_extra_mm"][1], 96.4)         # the whole shaft's estimate less what is preserved
        recon = geometry.Reconstruction(self.thing)
        self.assertEqual((recon["blown"], recon["far"], recon["hole3"], recon["hole5"]), ("proximal", "open", True, False))
        with self.assertRaisesRegex(ValueError, "edge_mm goes from 0.4 to 1"):
            geometry.Reconstruction(self.thing, edge_mm=2.0)
        with self.assertRaisesRegex(ValueError, "blown is one of proximal, distal"):
            geometry.Reconstruction(self.thing, blown="side")
        with self.assertRaisesRegex(ValueError, "96.4 mm further at most"):
            geometry.Reconstruction(self.thing, prox_extra_mm=60.0, dist_extra_mm=60.0)
        with self.assertRaisesRegex(ValueError, "nothing called 'length'"):
            geometry.Reconstruction(self.thing, length=200)

    def test_a_reconstruction_becomes_a_pipe(self):
        setup = geometry.build(geometry.Reconstruction(self.thing))
        self.assertEqual(setup.names, ["hole3", "hole1", "hole2"])     # from the mouth, which is the proximal end
        self.assertEqual(setup.ends_mm, (0.0, 113.6))
        self.assertAlmostEqual(setup.pipe.length, 0.1136)
        turned = geometry.build(geometry.Reconstruction(self.thing, blown="distal", hole5=True))
        self.assertEqual(turned.names, ["hole2", "hole5", "hole1", "hole3"])
        longer = geometry.build(geometry.Reconstruction(self.thing, prox_extra_mm=20.0, dist_extra_mm=30.0))
        self.assertAlmostEqual(longer.pipe.length, 0.1636)
        self.assertAlmostEqual(longer.pipe.holes[1][0] - setup.pipe.holes[1][0], 0.020)   # the holes stay where they are in the bone
        self.assertIn("proximal end +20 mm, distal end +30 mm", longer.recon.describe())

    def test_length_lips_and_a_hand_over_the_end_move_the_pitch_by_far_more_than_the_model_errs(self):
        base = self.lowest()
        self.assertTrue(1100 < base < 1400, base)                       # as found and open at both ends: a high whistle
        self.assertLess(cents(self.lowest(prox_extra_mm=40.0, dist_extra_mm=40.0), base), -600)
        self.assertLess(cents(self.lowest(mouth_open=0.02), base), -150)
        self.assertLess(cents(self.lowest(far="closed", mouth_open=0.02), base), -1200)   # a bottle, as Dimkaroski's low notes
        self.assertGreater(abs(cents(self.lowest(edge_mm=1.0), self.lowest(edge_mm=0.4))), 5)
        self.assertLess(abs(cents(self.lowest(edge_mm=1.0), self.lowest(edge_mm=0.4))), 120)

    def test_drawing_reconstructions_is_repeatable_and_stays_in_range(self):
        lim = geometry.limits(self.thing)
        first = [geometry.sample(self.thing, random.Random(7)).values for _ in range(1)][0]
        again = geometry.sample(self.thing, random.Random(7)).values
        self.assertEqual(first, again)
        rng = random.Random(3)
        for _ in range(60):
            v = geometry.sample(self.thing, rng, {"blown": "distal", "dist_extra_mm": 50.0}).values
            self.assertEqual((v["blown"], v["dist_extra_mm"]), ("distal", 50.0))
            self.assertLessEqual(v["prox_extra_mm"] + v["dist_extra_mm"], 96.4 + 1e-9)
            for key, (lo, hi) in lim.items():
                self.assertTrue(lo <= v[key] <= hi, (key, v[key]))

    def test_notes_and_intervals(self):
        setup = geometry.build(geometry.Reconstruction(self.thing))
        plain = scale.notes(setup, scale.ladder(setup.names))
        self.assertEqual([n.text for n in plain], ["xxx", "xxo", "xoo", "ooo"])
        self.assertEqual(sorted(n.hz for n in plain), [n.hz for n in plain])       # each hole opened raises the note
        self.assertEqual(len(scale.all_fingerings(setup.names)), 8)
        self.assertTrue(all(step > 50 for step in scale.steps(plain)))
        self.assertEqual(scale.nearest_ratio(702.0)[:2], (3, 2))
        self.assertEqual(scale.nearest_ratio(1200.0)[:2], (2, 1))
        self.assertAlmostEqual(scale.nearest_ratio(386.3)[2], 0.0, delta=0.1)      # 5/4
        self.assertEqual(scale.pitch_name(440.0), "A4+0")
        self.assertEqual(scale.pitch_name(446.4), "A4+25")


class ReportTest(unittest.TestCase):
    def test_the_reports_run_and_say_where_their_numbers_come_from(self):
        self.assertIn("divje-babe-1", bonepipe("objects").stdout)
        self.assertIn("every measurement has its source, page, method and confidence", bonepipe("check").stdout)
        shown = bonepipe("show", "divje-babe-1").stdout
        self.assertIn("length                                 113.6 mm  calipers  turk1997        161    high", shown)
        self.assertIn("status: contested", shown)
        self.assertIn("https://ojs.zrc-sazu.si/av/article/view/8291", shown)
        bore = bonepipe("bore", "divje-babe-1").stdout
        self.assertIn("108 CT slices (turk2005", bore)
        self.assertIn("hole1: the wall is open from 37.75 to 46.25 mm in the silhouettes, the middle at 42 mm", bore)
        self.assertIn("measured where the bone is a closed ring: 29 to 67.5 mm", bore)
        unknown = bonepipe("unknowns", "divje-babe-1").stdout
        self.assertIn("hole3_mm         9.35 mm  7.6 to 11.1", unknown)
        self.assertIn("blown          proximal     one of proximal, distal", unknown)

    def test_notes_for_one_reconstruction_and_the_spread_over_many(self):
        out = bonepipe("notes", "divje-babe-1").stdout
        self.assertIn("blown at the proximal end, 15% of it open; the other end open; hole1, hole2, hole3; 25 C", out)
        self.assertRegex(out, r"xxx +12\d\d\.\d Hz  D#6[-+]\d+ +q +\d+\n")
        self.assertRegex(out, r"xxo +14\d\d\.\d Hz  F6[-+]\d+ +q +\d+ +\+2\d\d c   \d+/\d+ [-+]\d+ c")
        self.assertIn("spread gives the range", out)
        turned = bonepipe("notes", "divje-babe-1", "blown=distal", "far=closed", "hole5=yes", "mouth_open=0.02").stdout
        self.assertIn("holes from the mouth: hole2 at", turned)
        self.assertIn("xxxx", turned)
        spread = bonepipe("spread", "divje-babe-1", "-n", "8", "-s", "2", "far=open").stdout
        self.assertIn("8 reconstructions drawn from the whole range of what is not known (seed 2), with far=open", spread)
        self.assertIn("the other end open", spread)
        self.assertNotIn("the other end closed", spread)
        self.assertIn("opening hole2", spread)
        self.assertEqual(spread, bonepipe("spread", "divje-babe-1", "-n", "8", "-s", "2", "far=open").stdout)   # a seed repeats
        self.assertIn("no object called 'flute'", bonepipe("notes", "flute", ok=False).stderr)
        self.assertIn("nothing called 'length'", bonepipe("notes", "divje-babe-1", "length=200", ok=False).stderr)
        self.assertIn("96.4 mm further at most", bonepipe("notes", "divje-babe-1", "prox_extra_mm=90", "dist_extra_mm=90", ok=False).stderr)
        self.assertIn("python3 -m bonepipe spread", bonepipe().stdout)


if __name__ == "__main__":
    unittest.main()
