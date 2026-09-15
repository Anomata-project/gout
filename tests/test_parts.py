import json
import math
import subprocess

from helpers import GoutTest, RATE, difference_peak_db, ffmpeg, gout_attr, samples
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
        self.assertEqual(json.loads((root / "gout.json").read_text())["tracks"][0]["parts"], parts())
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
