import json
import math
import subprocess

from helpers import GoutTest, RATE, difference_peak_db, ffmpeg, gout_attr, hits, samples
from test_ui import FakeScreen


def rms_db(values) -> float:
    return 20 * math.log10(math.sqrt(sum(v * v for v in values) / max(1, len(values))) or 1e-12)


class PartTest(GoutTest):
    def noise_project(self):
        """4 s of stereo pink noise placed at 1 s: cuts go at 2 s and 3.5 s on the timeline."""
        noise = self.tmp / "noise4.wav"
        ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:r=48000:a=0.5", "-ac", "2", "-c:a", "pcm_f32le", str(noise))
        root = self.project("song")
        self.gout("add", str(noise), "-a", "1s")
        return root

    def window(self, path, start, end, channel=0):
        chans = samples(path, 2)
        return chans[channel][int(start * RATE):int(end * RATE)]

    def test_a_cut_track_sounds_the_same_until_a_part_is_changed(self):
        root = self.noise_project()
        self.gout("mix")
        whole = root / "whole.wav"
        (root / "master.wav").replace(whole)
        self.gout("part", "1", "2s", "3.5s")
        self.gout("mix")
        self.assertLess(difference_peak_db(whole, root / "master.wav"), -120)  # the crossfades add back up

        self.gout("gain", "1", "p2", "-12")
        self.gout("mute", "1", "p3")
        self.gout("pan", "1", "p1", "L100")
        self.gout("mix")
        master = root / "master.wav"
        before, lowered = rms_db(self.window(master, 1.2, 1.9)), rms_db(self.window(master, 2.1, 3.4))
        self.assertAlmostEqual(before - lowered, 12, delta=1)  # balance: hard left keeps the left side as it was
        self.assertLess(rms_db(self.window(master, 3.6, 4.9)), -80)  # p3 is muted
        self.assertLess(rms_db(self.window(master, 1.2, 1.9, channel=1)), -80)  # p1 hard left
        self.assertGreater(rms_db(self.window(master, 2.1, 3.4, channel=1)), -60)  # p2 is not

    def test_live_playback_from_inside_a_part_matches_the_whole_track(self):
        root = self.noise_project()
        Project, build_graph = gout_attr("project", "Project"), gout_attr("mixer", "build_graph")

        def render(name, shift_ms):
            project = Project(root)
            inputs, graph, _ = build_graph(project, project.tracks(), shift_ms=shift_ms)
            out = self.tmp / name
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            for path in inputs:
                cmd += ["-i", str(path)]
            subprocess.run(cmd + ["-filter_complex", graph, "-map", "[mix]", "-c:a", "pcm_f32le", str(out)], check=True)
            return out

        wholes = {shift: render(f"whole{shift}.wav", shift) for shift in (1998, 2500)}
        self.gout("part", "1", "2s", "3.5s")
        for shift in (1998, 2500):  # inside the crossfade, and inside p2
            self.assertLess(difference_peak_db(wholes[shift], render(f"cut{shift}.wav", shift)), -120, shift)

    def test_parts_are_named_joined_and_undone(self):
        root = self.noise_project()
        parts = lambda: self.dump()["tracks"][0]["parts"]
        out = self.gout("part", "1", "3.5s", "2s").stdout  # in any order
        self.assertIn("3 parts", out)
        self.assertEqual([(p["in_ms"], p["out_ms"]) for p in parts()], [(0, 1000), (1000, 2500), (2500, 4000)])
        self.gout("part", "1", "p2", "name", "Chorus")
        self.gout("gain", "1", "chorus", "-6")
        self.assertEqual([(p["name"], p["gain_db"]) for p in parts()], [(None, 0), ("chorus", -6), (None, 0)])
        self.assertEqual(json.loads((root / "gout.json").read_text(encoding="utf-8"))["tracks"][0]["parts"], parts())
        for bad, why in (("on", "something else"), ("l30", "something else"), ("p5", "something else"),
                         ("warm", "preset of eq"), ("chorus", "already has")):
            self.assertIn(why, self.gout("part", "1", "p3" if bad == "chorus" else "p1", "name", bad, ok=False).stderr, bad)

        err = self.gout("part", "1", "join", ok=False).stderr
        self.assertIn("chorus (gain -6.0dB) has settings of its own", err)
        self.assertIn("not next to each other", self.gout("part", "1", "join", "p1", "p3", ok=False).stderr)
        self.gout("gain", "1", "p2", "0")
        self.gout("part", "1", "join", "p1", "chorus")  # alike now: the name of the first stays (none)
        self.assertEqual([(p["name"], p["in_ms"], p["out_ms"]) for p in parts()], [(None, 0, 2500), (None, 2500, 4000)])
        self.gout("part", "1", "join")
        self.assertEqual(parts(), [])  # one plain piece again
        self.gout("undo")
        self.assertEqual(len(parts()), 2)

        self.gout("mute", "1", "p2")
        self.assertIn("dropped p2 muted", self.gout("part", "1", "join", "-f").stdout)
        self.assertEqual(parts(), [])
        self.gout("undo")
        self.assertTrue(parts()[1]["mute"])

        self.assertIn("nothing to cut at 00:00:00.500", self.gout("part", "1", "0.5s", ok=False).stderr)
        self.assertIn("shorter than 20 ms", self.gout("part", "1", "3.510s", ok=False).stderr)  # 10 ms past the cut
        self.assertIn("works at the ui's prompt", self.gout("part", "1", "here", ok=False).stderr)
        self.assertIn("has no part 'p7'", self.gout("gain", "1", "p7", "-3", ok=False).stderr)
        self.assertIn("solo works on whole tracks", self.gout("solo", "1", "p1", ok=False).stderr)
        self.assertIn("a hard trim needs one piece", self.gout("trim", "1", "-H", "-st", "1s", ok=False).stderr)

        self.gout("trim", "1", "-et", "-500ms")  # the ends of a track in parts are its first and last part's
        self.assertEqual(parts()[-1]["out_ms"], 3500)
        self.assertIn("past the end of p1", self.gout("trim", "1", "-st", "3s", ok=False).stderr)

        before = self.dump()["tracks"][0]["parts"]
        (root / "gout.db").unlink()
        self.gout("rebuild")
        self.assertEqual(self.dump()["tracks"][0]["parts"], before)

    def test_part_here_cuts_at_the_playhead_in_the_ui(self):
        root = self.noise_project()
        Project, Tui = gout_attr("project", "Project"), gout_attr("tui", "Tui")
        project = Project(root)
        ui = Tui(project, FakeScreen())
        ui.playhead_ms = 2750
        ui.input = "part 1 here"
        ui.submit()
        self.assertEqual([(p["in_ms"], p["out_ms"]) for p in project.tracks()[0]["parts"]], [(0, 1750), (1750, 4000)])

    def test_effects_on_a_part_stay_in_that_part(self):
        root = self.noise_project()
        self.gout("part", "1", "2s", "3.5s")
        self.gout("eq", "1", "p2", "lp400")
        self.gout("mix")
        master = root / "master.wav"

        def highs_db(start, end):  # the change from sample to sample: mostly what is above a few kHz
            values = self.window(master, start, end)
            return rms_db([b - a for a, b in zip(values, values[1:])])

        p1, p2, p3 = highs_db(1.2, 1.9), highs_db(2.1, 3.4), highs_db(3.6, 4.9)
        self.assertGreater(p1 - p2, 15)
        self.assertAlmostEqual(p1, p3, delta=2)
        self.assertIn("has no part 'p9'", self.gout("eq", "1", "p9", "on", ok=False).stderr)

        self.gout("part", "1", "2.5s")  # a cut part keeps its effects on both sides
        chains = lambda: [[f"{i['kind']} {i['params']}" for i in p["fx"]] for p in self.dump()["tracks"][0]["parts"]]
        self.assertEqual(chains(), [[], ["eq lp400"], ["eq lp400"], []])
        self.gout("part", "1", "join", "p2", "p3")  # alike, so the eq stays
        self.assertEqual(chains(), [[], ["eq lp400"], []])
        self.assertIn("eq lp400", self.gout("part", "1", "join", ok=False).stderr)
        self.assertIn("dropped p2 eq lp400", self.gout("part", "1", "join", "-f").stdout)
        self.gout("undo")
        self.assertEqual(chains(), [[], ["eq lp400"], []])

        before = chains()
        (root / "gout.db").unlink()
        self.gout("rebuild")
        self.assertEqual(chains(), before)
        self.gout("rm", "1")
        import sqlite3
        db = sqlite3.connect(root / "gout.db")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fx").fetchone()[0], 0)  # no chain left behind
        db.close()

    def test_a_part_echo_rings_past_the_end_of_the_part(self):
        root = self.project("song", "click.wav")  # an impulse at 2.000 s
        self.gout("part", "1", "2.1s")
        self.gout("delay", "1", "p1", "250ms", "w100", "f0", "n1")
        self.gout("mix")
        found = [round(t, 3) for t, _ in hits(root / "master.wav")]
        self.assertEqual(found, [2.0, 2.25])  # the echo comes after p1 has ended

    def test_a_part_moves_and_is_trimmed_where_you_hear_it(self):
        root = self.project("song", "click.wav")  # an impulse at 2.000 s
        clicks = lambda: [round(t, 3) for t, _ in hits(root / "master.wav")] if self.gout("mix") else []
        self.gout("part", "1", "1.5s")
        self.gout("move", "1", "p2", "+1s")  # the click goes with its part
        self.assertEqual(clicks(), [3.0])
        self.gout("trim", "1", "p2", "-st", "+250ms", "-et", "3.1s")  # less of the part, still where it was
        self.assertEqual(clicks(), [3.0])
        self.assertEqual([(p["in_ms"], p["out_ms"], p["shift_ms"]) for p in self.dump()["tracks"][0]["parts"]],
                         [(0, 1500, 0), (1750, 2100, 1000)])
        self.gout("trim", "1", "p2", "-st", "3.05s")
        self.assertEqual(self.gout("mix").returncode, 0)
        self.assertEqual(hits(root / "master.wav"), [])  # it starts after the click now
        self.gout("undo")
        self.gout("move", "1", "p2", "1s")  # placed: the part begins at 1 s, the click 0.25 s into it
        self.assertEqual(clicks(), [1.25])
        self.assertIn("cannot start before its file does",  # its file begins at -0.75 s now
                      self.gout("trim", "1", "p2", "-st", "-2s", ok=False).stderr)
        self.assertIn("has no part 'p9'", self.gout("move", "1", "p9", "+1s", ok=False).stderr)

        self.gout("rm", "1", "p1")
        self.assertIn("all that is left of track 1", self.gout("rm", "1", "p1", ok=False).stderr)
        self.assertEqual(clicks(), [1.25])
        self.gout("undo")
        self.assertEqual(len(self.dump()["tracks"][0]["parts"]), 2)

    def test_a_moved_part_fades_at_its_own_edges(self):
        root = self.noise_project()
        self.gout("part", "1", "2s")
        self.gout("move", "1", "p2", "+500ms")  # p1 now stops at 2 s and p2 starts at 2.5 s, with silence between
        self.gout("mix")
        (left,) = samples(root / "master.wav", 1)
        loud = max(abs(v) for v in left[int(1.5 * RATE):int(1.9 * RATE)])
        self.assertLess(max(abs(v) for v in left[int(1.9995 * RATE):int(2.0 * RATE)]), 0.15 * loud)  # p1 fades out
        self.assertLess(max(abs(v) for v in left[int(2.5 * RATE):int(2.5005 * RATE)]), 0.15 * loud)  # p2 fades in
        self.assertLess(max(abs(v) for v in left[int(2.01 * RATE):int(2.49 * RATE)]), 1e-6)

    def test_the_timeline_sheet_and_completion_know_the_parts(self):
        root = self.noise_project()
        self.gout("part", "1", "2s", "3.5s")
        self.gout("part", "1", "p2", "name", "chorus")
        self.gout("eq", "1", "chorus", "lp400")
        view = self.gout("view", "-w", "100").stdout
        for mark in ("╷p1", "╷chorus", "╷p3"):
            self.assertIn(mark, view)

        Project, Tui = gout_attr("project", "Project"), gout_attr("tui", "Tui")
        project = Project(root)
        ui = Tui(project, FakeScreen())
        rows = {r["id"]: r for r in ui.sheet_build() if r["id"]}
        chorus = project.tracks()[0]["parts"][1]
        ref = f"p#{chorus['id']}"
        self.assertEqual(rows[f"{ref}:gain"]["copy"], "gain 1 chorus 0")
        self.assertEqual(rows[f"{ref}:at"]["copy"], "move 1 chorus =00:00:02.000")
        self.assertEqual(rows[f"fx#{chorus['fx'][0]['id']}"]["copy"], "eq 1 chorus lp400")
        ui.handle("\x05")
        ui.sheet_rows = ui.sheet_build()
        ui.sheet_cur = next(i for i, r in enumerate(ui.sheet_rows) if r["id"] == f"{ref}:at")
        for ch in "+1s":
            ui.handle(ch)
        ui.sheet_cur = next(i for i, r in enumerate(ui.sheet_rows) if r["id"] == f"{ref}:gain")
        for ch in "-3":
            ui.handle(ch)
        ui.handle("\x18")  # both apply, though the move changes which part is p2
        moved = next(p for p in project.tracks()[0]["parts"] if p["id"] == chorus["id"])
        self.assertEqual((moved["shift_ms"], moved["gain_db"]), (1000, -3.0))

        for typed, completed in (("gain 1 ch", "gain 1 chorus "), ("eq 1 cho", "eq 1 chorus "), ("move 1 p3", "move 1 p3 ")):
            ui.input = typed
            ui.handle("\t")
            self.assertEqual(ui.input, completed, typed)
