import os
import time

from helpers import GoutTest, gout_attr, hits


class LoopTest(GoutTest):
    def played(self, *words):
        """What gout play sends to the speakers, written to a file: the first stretch and two rounds."""
        out = self.tmp / "heard.wav"
        out.unlink(missing_ok=True)
        self.more_env["GOUT_PLAYER"] = f"file:{out}"
        said = self.gout("play", *words).stdout
        for _ in range(50):  # the file player writes on after gout has handed it over
            if out.exists() and out.stat().st_size > 1000:
                time.sleep(0.3)
                break
            time.sleep(0.1)
        return said, [round(t, 3) for t, _ in hits(out)]

    def test_playing_goes_round_the_loop_without_a_gap(self):
        root = self.project("song", "click.wav")  # an impulse at 2.000 s
        self.assertIn("none", self.gout("loop").stdout)
        self.assertIn("on: playing goes round it", self.gout("lo", "1.5s", "2.5s").stdout)
        said, clicks = self.played()  # from outside the loop: from its start
        self.assertIn("round the loop 00:00:01.500 -> 00:00:02.500", said)
        self.assertEqual(clicks, [0.5, 1.5, 2.5])  # a click every second, the loop's length
        said, clicks = self.played("2.2s")  # from inside it: to its end first
        self.assertEqual(clicks, [0.8, 1.8])
        self.gout("mix")
        said, clicks = self.played()  # master.wav loops the same way
        self.assertIn("master.wav, round the loop", said)
        self.assertEqual(clicks, [0.5, 1.5, 2.5])

        self.assertIn("off (loop on brings it back)", self.gout("loop", "off").stdout)
        said, clicks = self.played("1.9s")
        self.assertNotIn("round the loop", said)
        self.assertIn("50 ms at least", self.gout("loop", "2s", "1s", ok=False).stderr)
        self.gout("loop", "on")
        self.assertIn("━", self.gout("view", "-w", "80").stdout)  # the loop on the timeline's ruler

    def test_the_position_goes_round_and_a_take_never_loops(self):
        Player = gout_attr("player", "Player")
        player = Player(None, 2.2, 3.0, stream=["-i", "x", "-filter_complex", "[0:a]anull[live]", "-map", "[live]"],
                        loop=(1.5, 2.5), stream_start=1.5)
        player.backend, player.t0 = "null", time.monotonic()
        for elapsed, where in ((0.1, 2.3), (0.3, 1.5), (0.8, 2.0), (1.35, 1.55)):
            player.t0 = time.monotonic() - elapsed
            self.assertAlmostEqual(player.position(), where, delta=0.02, msg=elapsed)
        args = " ".join(player.source_args())
        self.assertIn("atrim=start=0.700000:end=1.000000", args)  # to the loop's end, in the stream's own time
        self.assertIn("aloop=loop=-1:size=48000", args)

        root = self.project("song", "click.wav")
        self.gout("loop", "1s", "3s")
        Project, player_for = gout_attr("project", "Project"), gout_attr("commands", "player_for")
        os.environ["GOUT_PLAYER"] = "null"
        self.addCleanup(os.environ.pop, "GOUT_PLAYER", None)
        project = Project(root)
        self.assertIn("aloop", " ".join(player_for(project, 0).source_args()))
        self.assertNotIn("aloop", " ".join(player_for(project, 0, loop=False).source_args()))  # what a take plays along to

    def test_play_from_to_stops_there_and_does_not_loop(self):
        root = self.project("song", "click.wav")
        self.gout("loop", "1.5s", "2.5s")
        out = self.tmp / "heard.wav"
        self.more_env["GOUT_PLAYER"] = f"file:{out}"
        said = self.gout("play", "1.8s", "2.3s").stdout
        self.assertIn("to 00:00:02.300", said)
        self.assertNotIn("round the loop", said)
        time.sleep(1.0)
        from helpers import duration
        self.assertAlmostEqual(duration(out), 0.5, delta=0.03)
        self.assertEqual([round(t, 2) for t, _ in hits(out)], [0.2])
        self.assertIn("is not after", self.gout("play", "2s", "1s", ok=False).stderr)

    def test_duplicate_puts_a_stretch_on_a_new_track_at_the_same_time(self):
        root = self.project("song", "click.wav")
        from helpers import peak_db, samples
        self.gout("mix")
        (single,) = samples(root / "master.wav", 1)
        out = self.gout("dup", "1", "1.5s", "2.5s").stdout
        self.assertIn("click-copy", out)
        track = self.dump()["tracks"][1]
        self.assertEqual((track["name"], track["offset_ms"], track["length_ms"]), ("click-copy", 1500, 1000))
        self.gout("mix")
        (doubled,) = samples(root / "master.wav", 1)
        self.assertAlmostEqual(peak_db(doubled) - peak_db(single), 6.0, delta=0.1)  # the copy sits sample on sample
        self.gout("undo")
        self.assertFalse((root / "master" / "click-copy.wav").exists())
        self.assertIn("plays nothing", self.gout("dup", "1", "5s", "6s", ok=False).stderr)
        self.gout("part", "1", "2s")
        self.gout("move", "1", "p2", "+300ms")
        self.assertIn("moved apart", self.gout("dup", "1", "1.5s", "2.5s", ok=False).stderr)
        self.gout("dup", "1", "2.3s", "2.8s", "-a", "5s", "-n", "later")
        self.assertEqual(self.dump()["tracks"][-1]["offset_ms"], 5000)
