import math
import os
import unittest
from array import array

from helpers import GoutTest, RATE, duration, gout_attr, samples
from test_ui import FakeScreen


def window(x, lo: float, hi: float):
    return x[round(lo * RATE):round(hi * RATE)]


def peak(x, lo: float, hi: float) -> float:
    return max((abs(v) for v in window(x, lo, hi)), default=0.0)


def pitch(x, lo: float, hi: float) -> float:
    """The frequency between lo and hi seconds, from the places the wave crosses zero going up."""
    seg = window(x, lo, hi)
    crossings = [i + seg[i] / (seg[i] - seg[i + 1]) for i in range(len(seg) - 1) if seg[i] <= 0 < seg[i + 1]]
    return (len(crossings) - 1) * RATE / (crossings[-1] - crossings[0])


def db(ratio: float) -> float:
    return 20 * math.log10(ratio)


BEEP = '''
import math
from array import array
from gout.inst import Instrument
from gout.pattern import NOTE, Feature, note_hz


class Beep(Instrument):
    name = "beep"
    summary = "a bare sine for every note"
    features = (NOTE, Feature("loud", "toggle", False, summary="twice as loud"))
    settings = (Feature("octave", "count", 0, 0, 3, summary="octaves up"),)

    def render(self, ctx, pattern, settings):
        edges = ctx.edges(pattern)
        out = array("f", bytes(4 * edges[-1]))
        for step, midi, steps in pattern.notes():
            hz = note_hz(midi) * 2 ** self.value(settings, "octave")
            gain = 0.5 if pattern.value(self.features[1], step) else 0.25
            start = edges[step - 1]
            for i in range(start, edges[step - 1 + steps]):
                out[i] = gain * math.sin(2 * math.pi * hz * (i - start) / ctx.rate)
        return [out]


def register(gout):
    gout.requires(2)
    gout.add_instrument(Beep())
'''


class PatternTest(unittest.TestCase):
    def test_notes_read_and_written(self):
        parse_note, note_name, note_hz = (gout_attr("pattern", n) for n in ("parse_note", "note_name", "note_hz"))
        self.assertEqual([parse_note(w) for w in ("C4", "a4", "Bb1", "A#1", "c-1", "G9")], [60, 69, 34, 34, 0, 127])
        self.assertEqual([note_name(m) for m in (60, 34, 0)], ["C4", "A#1", "C-1"])
        self.assertAlmostEqual(note_hz(57), 220.0)
        for bad in ("H2", "C", "C#", "A10", "2C"):
            with self.assertRaises(ValueError):
                parse_note(bad)

    def test_a_hold_lengthens_the_note_before_and_a_rest_ends_it(self):
        Pattern, NOTE = gout_attr("pattern", "Pattern"), gout_attr("pattern", "NOTE")
        p = Pattern(8)
        p.put(NOTE, None, 40)                 # every step
        for step, word in ((2, "="), (3, "="), (5, "rest"), (6, "="), (8, "C3")):
            p.put(NOTE, [step], NOTE.parse(word))
        # a hold after a rest holds nothing; steps without a cell take the row's note
        self.assertEqual(p.notes(), [(1, 40, 3), (4, 40, 1), (7, 40, 1), (8, 48, 1)])
        self.assertEqual(Pattern.from_json(p.to_json(), (NOTE,)).notes(), p.notes())
        self.assertEqual(p.resize(4), 3)
        self.assertEqual(p.notes(), [(1, 40, 3), (4, 40, 1)])

    def test_step_words_and_cell_values(self):
        parse_steps, Feature = gout_attr("pattern", "parse_steps"), gout_attr("pattern", "Feature")
        self.assertEqual(parse_steps("5-8", 16), [5, 6, 7, 8])
        self.assertEqual(parse_steps("9,1,5", 16), [1, 5, 9])
        self.assertIsNone(parse_steps("all", 16))
        for bad in ("0", "17", "8-5", "x", "1-"):
            with self.assertRaises(ValueError):
                parse_steps(bad, 16)
        ms = Feature("decay", "number", 400.0, 0, 20000, "ms")
        self.assertEqual([ms.parse(w) for w in ("200", "200ms", "1.5k", "0")], [200.0, 200.0, 1500.0, 0.0])
        with self.assertRaisesRegex(ValueError, "from 0 to 20000 ms"):
            ms.parse("-1")
        with self.assertRaisesRegex(ValueError, "one of saw, sine"):
            Feature("wave", "choice", "saw", choices=("saw", "sine")).parse("round")

    def test_steps_do_not_drift(self):
        step_edges, step_seconds = gout_attr("pattern", "step_edges"), gout_attr("pattern", "step_seconds")
        step = step_seconds(133.0, "1/16t")
        edges = step_edges(96, step, 44100)
        self.assertEqual(edges[-1], round(96 * step * 44100))
        self.assertLessEqual(max(b - a for a, b in zip(edges, edges[1:])) - min(b - a for a, b in zip(edges, edges[1:])), 1)

    def test_waves_are_in_tune_and_a_loop_carries_its_tail_over(self):
        wave, tile = gout_attr("synthkit", "wave"), gout_attr("synthkit", "tile")
        for shape in ("sine", "saw", "square", "triangle"):
            x = wave(shape, 220.0, RATE // 2, RATE)
            self.assertAlmostEqual(pitch(x, 0.0, 0.5), 220.0, delta=0.5, msg=shape)
            self.assertLess(max(abs(v) for v in x), 1.2, shape)
        one = array("f", [1.0, 2.0, 3.0, 4.0, 0.5, 0.25])          # four samples of pattern, two that ring on
        self.assertEqual(list(tile(one, 4, 3)), [1.0, 2.0, 3.0, 4.0, 1.5, 2.25, 3.0, 4.0, 1.5, 2.25, 3.0, 4.0, 0.5, 0.25])
        self.assertEqual(list(tile(array("f", [1.0, 2.0]), 4, 2)), [1.0, 2.0, 0.0, 0.0, 1.0, 2.0, 0.0, 0.0])


class InstrumentTest(GoutTest):
    def lead(self, *edits: tuple) -> array:
        """A project with a sine synth called lead, the edits applied; the track's samples."""
        self.root = self.project("song")
        self.gout("instrument", "add", "synth", "lead")
        self.gout("ins", "lead", "wave", "sine")
        return self.edit(*edits)

    def edit(self, *edits: tuple) -> array:
        for words in edits:
            self.gout("ins", "lead", *words)
        return samples(self.root / "master" / "lead.wav")[0]

    def test_a_new_instrument_track_is_a_track_with_a_pattern(self):
        root = self.project("song", "tone.wav")
        out = self.gout("instrument", "add", "synth", "bass", "-a", "4s").stdout
        self.assertIn("set   bpm 120", out)
        self.assertIn("synth  16 steps of 1/16 at 120 bpm = 2.000 s  at 00:00:04.000", out)
        doc = self.dump()
        self.assertEqual(doc["project"]["bpm"], "120")
        track = doc["tracks"][1]
        self.assertEqual((track["name"], track["file"], track["kind"], track["offset_ms"], track["length_ms"]),
                         ("bass", "bass.wav", "wav", 4000, 2000))
        self.assertEqual(track["instrument"], {"kind": "synth", "settings": {}, "pattern": {"steps": 16, "rows": {}}})
        self.assertAlmostEqual(duration(root / "master" / "bass.wav"), 2.0, places=3)
        self.assertEqual(peak(samples(root / "master" / "bass.wav")[0], 0, 2), 0.0)
        self.assertIn("synth 16 steps", self.gout("ls").stdout)
        self.assertIn("bass             synth  16 steps", self.gout("instrument").stdout)
        self.assertNotIn("set   bpm", self.gout("ins", "add", "syn").stdout)   # the tempo is there now; syn is the short name
        self.assertEqual(self.dump()["tracks"][2]["name"], "synth")

    def test_notes_sound_at_their_pitch_on_their_step(self):
        x = self.lead(("decay", "0"), ("release", "20"), ("note", "1", "A3"), ("note", "5", "A4"))
        self.assertAlmostEqual(pitch(x, 0.01, 0.12), 220.0, delta=1.0)
        self.assertAlmostEqual(pitch(x, 0.51, 0.62), 440.0, delta=2.0)
        self.assertAlmostEqual(peak(x, 0.05, 0.12), 0.5, delta=0.01)           # level 100 is -6 dBFS
        self.assertLess(peak(x, 0.15, 0.4995), 1e-6)                          # nothing between the notes
        onset = next(i for i in range(round(0.2 * RATE), len(x)) if abs(x[i]) > 0.01) / RATE
        self.assertAlmostEqual(onset, 0.500, delta=0.001)                     # step 5 of sixteenths at 120 bpm
        x = self.edit(("level", "5", "50"))
        self.assertAlmostEqual(peak(x, 0.55, 0.62), 0.25, delta=0.01)
        self.assertAlmostEqual(peak(x, 0.05, 0.12), 0.5, delta=0.01)

    def test_decay_and_release_shape_a_note(self):
        x = self.lead(("steps", "4"), ("step", "1/4"), ("note", "1", "A3"), ("decay", "200"), ("release", "100"))
        self.assertAlmostEqual(len(x) / RATE, 2.0, places=3)                   # four quarter notes
        # a decay of 200 ms is 60 dB in 200 ms: 30 dB between two moments 100 ms apart
        self.assertAlmostEqual(db(peak(x, 0.1475, 0.1525) / peak(x, 0.0475, 0.0525)), -30.0, delta=1.0)
        x = self.edit(("decay", "0"))
        self.assertAlmostEqual(db(peak(x, 0.44, 0.45) / peak(x, 0.04, 0.05)), 0.0, delta=0.1)
        self.assertAlmostEqual(peak(x, 0.5475, 0.5525), 0.25, delta=0.04)      # half way down the release
        self.assertLess(peak(x, 0.6005, 2.0), 1e-6)                           # and gone after it
        x = self.edit(("release", "1", "400"))                                # the step's own release
        self.assertGreater(peak(x, 0.75, 0.8), 0.1)

    def test_hold_rest_and_the_rows_value_for_every_step(self):
        x = self.lead(("steps", "4"), ("step", "1/4"), ("decay", "0"), ("release", "20"), ("note", "1", "A3", "="))
        self.assertAlmostEqual(peak(x, 0.9, 0.95), 0.5, delta=0.01)            # still sounding through step 2
        self.assertLess(peak(x, 1.05, 2.0), 1e-6)
        x = self.edit(("note", "A4"), ("note", "3", "rest"))                  # A4 wherever a step has nothing of its own
        self.assertAlmostEqual(pitch(x, 0.1, 0.9), 220.0, delta=1.0)
        self.assertLess(peak(x, 1.05, 1.49), 1e-6)
        self.assertAlmostEqual(pitch(x, 1.55, 1.95), 440.0, delta=2.0)
        x = self.edit(("note", "all", "-"), ("note", "3", "."))               # empty again
        self.assertLess(peak(x, 1.05, 2.0), 1e-6)
        self.assertEqual(self.dump()["tracks"][0]["instrument"]["pattern"]["rows"]["note"],
                         {"all": None, "cells": {"1": 57, "2": "="}})

    def test_loop_repeats_the_pattern_and_steps_set_its_length(self):
        x = self.lead(("note", "1", "A3"), ("loop", "3"))
        self.assertEqual(len(x), 6 * RATE)
        self.assertEqual(list(x[:6000]), list(x[2 * RATE:2 * RATE + 6000]))
        self.assertEqual(list(x[:6000]), list(x[4 * RATE:4 * RATE + 6000]))
        self.assertIn("2.000 s, 3 times", self.gout("ins", "lead").stdout)
        self.assertEqual(len(self.edit(("steps", "8"))), 3 * RATE)
        self.assertEqual(len(self.edit(("steps", "+8"))), 6 * RATE)
        self.edit(("note", "16", "C3"))
        self.assertIn("steps 12  (1 cell past the end dropped)", self.gout("ins", "lead", "steps", "12").stdout)
        self.assertEqual(self.dump()["tracks"][0]["length_ms"], 4500)

    def test_what_rings_on_after_the_last_step_sounds_over_the_next_pass(self):
        x = self.lead(("decay", "0"), ("note", "16", "A3"), ("release", "16", "200"))
        self.assertEqual(len(x), round(2.2 * RATE))                            # the file is longer by the tail
        x = self.edit(("loop", "2"))
        self.assertEqual(len(x), round(4.2 * RATE))
        self.assertGreater(peak(x, 2.01, 2.05), 0.1)                           # step 1 is empty, the tail is heard there
        self.assertLess(peak(x, 0.0, 1.87), 1e-6)

    def test_undo_brings_back_the_pattern_and_the_sound(self):
        x = self.lead(("decay", "0"), ("note", "1", "A3"), ("note", "1", "A4"))
        self.assertAlmostEqual(pitch(x, 0.01, 0.12), 440.0, delta=2.0)
        self.assertIn("undo  instrument lead note 1 A4", self.gout("undo").stdout)
        x = samples(self.root / "master" / "lead.wav")[0]
        self.assertAlmostEqual(pitch(x, 0.01, 0.12), 220.0, delta=1.0)
        self.assertEqual(self.dump()["tracks"][0]["instrument"]["pattern"]["rows"]["note"]["cells"], {"1": 57})
        for _ in range(4):                                                    # note, decay, wave, and the track itself
            self.gout("undo")
        doc = self.dump()
        self.assertEqual(doc["tracks"], [])
        self.assertNotIn("bpm", doc["project"])                               # the tempo came with the track
        self.assertFalse((self.root / "master" / "lead.wav").exists())

    def test_the_tempo_and_the_sample_rate_write_the_track_again(self):
        self.lead(("note", "1", "A3"))
        self.gout("set", "bpm", "60")
        self.assertAlmostEqual(duration(self.root / "master" / "lead.wav"), 4.0, places=3)
        self.assertEqual(self.dump()["tracks"][0]["length_ms"], 4000)
        self.assertIn("plays its pattern at the tempo", self.gout("set", "bpm", "off", ok=False).stderr)
        self.gout("set", "rate", "44100")
        x = samples(self.root / "master" / "lead.wav", rate=44100)[0]
        self.assertEqual(len(x), 4 * 44100)
        self.assertEqual(self.dump()["tracks"][0]["sample_rate"], 44100)

    def test_gout_json_carries_the_pattern_and_rebuilds_the_track(self):
        self.lead(("decay", "0"), ("note", "1", "A3"), ("loop", "2"))
        self.gout("move", "lead", "3s")
        self.assertEqual(self.dump()["tracks"][0]["instrument"],
                         {"kind": "synth", "settings": {"loop": 2, "wave": "sine"},
                          "pattern": {"steps": 16, "rows": {"decay": {"all": 0.0, "cells": {}},
                                                           "note": {"all": None, "cells": {"1": 57}}}}})
        (self.root / "gout.db").unlink()
        (self.root / "master" / "lead.wav").unlink()                           # nothing but gout.json is left of the track
        self.gout("rebuild")
        track = self.dump()["tracks"][0]
        self.assertEqual((track["name"], track["offset_ms"], track["length_ms"]), ("lead", 3000, 4000))
        x = samples(self.root / "master" / "lead.wav")[0]
        self.assertAlmostEqual(pitch(x, 2.01, 2.12), 220.0, delta=1.0)
        # and another project takes the track from the document alone
        self.cwd = self.tmp
        self.gout("new", "other")
        self.cwd = self.tmp / "other"
        self.gout("import", str(self.root / "gout.json"))
        self.assertEqual(self.dump()["tracks"][0]["instrument"]["pattern"]["rows"]["note"]["cells"], {"1": 57})
        self.assertAlmostEqual(pitch(samples(self.cwd / "master" / "lead.wav")[0], 0.01, 0.12), 220.0, delta=1.0)

    def test_it_is_mixed_like_any_track(self):
        x = self.lead(("decay", "0"), ("note", "1", "A3", "=", "=", "="))
        self.gout("mix")
        left, right = samples(self.root / "master.wav", 2)
        self.assertAlmostEqual(pitch(left, 0.05, 0.45), 220.0, delta=1.0)
        self.assertAlmostEqual(peak(left, 0.1, 0.4), peak(x, 0.1, 0.4), delta=0.005)   # mono goes to both sides at full level
        self.assertEqual(list(left[4800:4900]), list(right[4800:4900]))
        self.gout("gain", "lead", "-6")
        self.gout("eq", "lead", "hp80")
        self.gout("mix")
        self.assertAlmostEqual(db(peak(samples(self.root / "master.wav", 2)[0], 0.1, 0.4) / 0.5), -6.0, delta=0.5)
        self.assertIn("fx eq hp80", self.gout("ls").stdout)

    def test_a_wav_that_went_missing_is_written_again(self):
        self.lead(("decay", "0"), ("note", "1", "A3"))
        wav = self.root / "master" / "lead.wav"
        wav.unlink()
        self.assertIn("nothing new, nothing missing", self.gout("scan").stdout)
        self.assertAlmostEqual(pitch(samples(wav)[0], 0.01, 0.12), 220.0, delta=1.0)

    def test_wrong_words_say_what_is_wrong_and_change_nothing(self):
        root = self.project("song", "tone.wav")
        self.gout("ins", "add", "synth", "lead")
        self.gout("ins", "lead", "note", "1", "C2")
        before = self.dump()
        for words, says in ((("ins", "lead", "bogus", "1", "2"), "no row or cell called 'bogus'"),
                            (("ins", "lead", "note", "1", "H2"), "is not a note"),
                            (("ins", "lead", "note", "17", "C2"), "the pattern has steps 1 to 16"),
                            (("ins", "lead", "note", "15", "C2", "D2", "E2"), "run past step 16"),
                            (("ins", "lead", "note", "1-2", "C2", "D2"), "from one step on"),
                            (("ins", "lead", "steps", "0"), "whole number from 1 to 256"),
                            (("ins", "lead", "steps", "+300"), "whole number from 1 to 256"),
                            (("ins", "lead", "loop", "0"), "whole number from 1 to 999"),
                            (("ins", "lead", "wave", "round"), "one of sine, saw, square, triangle"),
                            (("ins", "lead", "decay", "1", "-5"), "from 0 to 20000 ms"),
                            (("ins", "lead", "note"), "note what?"),
                            (("ins", "tone", "note", "1", "C2"), "is a recording, not an instrument"),
                            (("ins", "add", "nosuch"), "no instrument called 'nosuch'"),
                            (("trim", "lead", "-H", "-st", "100ms"), "is an instrument track")):
            self.assertIn(says, self.gout(*words, ok=False).stderr, words)
        self.assertEqual(self.dump(), before)
        self.assertTrue((root / "master" / "lead.wav").exists())

    def test_the_pattern_reads_as_a_grid(self):
        self.project("song")
        self.gout("ins", "add", "synth", "bass")
        for words in (("note", "1", "C2", "-", "C2", "D#2", ".", "C3", "-", "A#1"), ("note", "9-12", "G2"), ("note", "14", "="),
                      ("decay", "200"), ("decay", "4", "800"), ("level", "1,5,9,13", "100"), ("level", "60"),
                      ("title", "acid", "line"), ("description", "a", "tight", "one")):
            self.gout("ins", "bass", *words)
        out = self.gout("instrument", "bass").stdout.splitlines()
        self.assertEqual(out[1:], [
            "      title acid line   description a tight one   wave saw",
            "              all │   1   2   3   4 │   5   6   7   8 │   9  10  11  12 │  13  14  15  16",
            "      note        │  C2   ·  C2 D#2 │   ·  C3   · A#1 │  G2  G2  G2  G2 │   ·   =   ·   ·",
            "      decay   200 │   ·   ·   · 800 │   ·   ·   ·   · │   ·   ·   ·   · │   ·   ·   ·   ·",
            "      release  50 │   ·   ·   ·   · │   ·   ·   ·   · │   ·   ·   ·   · │   ·   ·   ·   ·",
            "      level    60 │ 100   ·   ·   · │ 100   ·   ·   · │ 100   ·   ·   · │ 100   ·   ·   ·",
        ])
        self.gout("ins", "bass", "steps", "20")
        self.assertIn("                  │  17  18  19  20", self.gout("ins", "bass").stdout.splitlines())
        self.gout("ins", "bass", "title", "-")                                # no title of its own: the track's name
        self.assertIn("      title bass   description a tight one   wave saw", self.gout("ins", "bass").stdout.splitlines())

    def test_listing_the_instruments_needs_no_project(self):
        out = self.gout("instrument", "kinds").stdout
        self.assertIn("synth      syn", out)
        self.assertIn("rows: note decay release level   cells: wave", out)
        self.assertIn("not inside a gout project", self.gout("instrument", ok=False).stderr)
        self.assertIn("gout instrument ins add KIND", self.gout("help", "ins").stdout)
        self.assertIn("row  decay (ms)", self.gout("help").stdout)
        self.assertIn("instrument ins", self.gout("cheat", "-w", "120").stdout)

    def test_an_addon_adds_an_instrument(self):
        addon = self.addons / "beep.py"
        addon.write_text(BEEP, encoding="utf-8")
        (self.addons / "clash.py").write_text(
            "from gout.inst import Instrument\nclass E(Instrument):\n    name = 'eq'\n"
            "def register(gout):\n    gout.add_instrument(E())\n", encoding="utf-8")
        (self.addons / "later.py").write_text("def register(gout):\n    gout.requires(3)\n", encoding="utf-8")
        root = self.project("song")
        result = self.gout("instrument", "kinds")
        self.assertIn("beep              a bare sine for every note   [addon beep.py]", result.stdout)
        self.assertIn("'eq' is already taken", result.stderr)
        self.assertIn("it needs gout's addon API 3, and this gout has 2", result.stderr)
        self.assertIn("beep.py                  beep (instrument)", self.gout("addons").stdout)
        self.gout("ins", "add", "beep", "t")
        for words in (("note", "1", "A3"), ("note", "5", "A3"), ("loud", "5", "on"), ("octave", "1")):
            self.gout("ins", "t", *words)
        x = samples(root / "master" / "t.wav")[0]
        self.assertAlmostEqual(pitch(x, 0.01, 0.12), 440.0, delta=2.0)
        self.assertAlmostEqual(peak(x, 0.01, 0.12), 0.25, delta=0.01)
        self.assertAlmostEqual(peak(x, 0.51, 0.62), 0.5, delta=0.01)
        addon.unlink()                                                        # the project without its addon
        self.assertIn("beep (not installed)", self.gout("instrument").stdout)
        self.assertIn("which is not installed", self.gout("ins", "t", "note", "1", "C2", ok=False).stderr)
        self.assertIn("instruments that are not installed: beep", self.gout("addons").stdout)
        self.gout("mix")                                                      # it sounds as it was last written
        self.assertAlmostEqual(pitch(samples(root / "master.wav")[0], 0.01, 0.12), 440.0, delta=2.0)
        self.assertEqual(self.dump()["tracks"][0]["instrument"]["settings"], {"octave": 1})

    def test_tab_completes_instrument_words_in_the_ui(self):
        root = self.project("song", "tone.wav")
        self.gout("ins", "add", "synth", "lead")
        os.environ["GOUT_ADDONS"] = str(self.addons)
        ui = gout_attr("tui", "Tui")(gout_attr("project", "Project")(root), FakeScreen())
        self.assertEqual(ui.complete_words(["instrument"], ""), ["add", "kinds", "lead"])
        self.assertEqual(ui.complete_words(["ins", "add"], "s"), ["synth"])
        self.assertEqual(ui.complete_words(["ins", "lead"], "de"), ["decay", "description"])
        self.assertEqual(ui.complete_words(["ins", "lead", "wave"], "s"), ["sine", "saw", "square"])
        self.assertEqual(ui.complete_words(["ins", "lead", "note"], ""), ["all"])
        self.assertIn("instrument", ui.complete_words([], "ins"))
        for ch in "ins lead note 1 A3":
            ui.handle(ch)
        ui.handle("\n")
        self.assertTrue(any("note 1: A3" in line for line in ui.log), ui.log[-3:])
        ui.handle("\x15")                                                     # ctrl-u
        self.assertEqual(self.dump()["tracks"][1]["instrument"]["pattern"]["rows"], {})


if __name__ == "__main__":
    unittest.main()
