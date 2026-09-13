import os
import time

from helpers import GoutTest, gout_attr
from test_ui import FakeScreen


class PlayTest(GoutTest):
    def test_play_streams_a_stale_project_live_and_takes_real_time(self):
        root = self.project("song", "click.wav")  # 4 s
        started = time.monotonic()
        out = self.gout("play", "2.5s").stdout
        self.assertIn("live", out)
        self.assertIn("finished", out)
        self.assertGreater(time.monotonic() - started, 1.4)  # 1.5 s of audio
        self.assertFalse((root / "master.wav").exists())  # nothing rendered
        out = self.gout("play", "3s", "-r").stdout  # -r renders first, then plays the file
        self.assertIn("rendering", out)
        self.assertNotIn("live", out)
        self.assertTrue((root / "master.wav").exists())
        self.assertNotIn("live", self.gout("play", "3.5s").stdout)  # current: the file
        self.gout("gain", "1", "-3")
        self.assertIn("live", self.gout("play", "3.5s").stdout)  # a change: live again
        self.gout("play", "10s", ok=False)
        self.gout("stop", ok=False)  # only means something in the ui

    def test_space_plays_and_stops_with_a_playhead(self):
        root = self.project("song", "click.wav")
        os.environ["GOUT_PLAYER"] = "null"
        os.environ["GOUT_ADDONS"] = str(self.addons)
        project = gout_attr("project", "Project")(root)
        screen = FakeScreen(30, 120)
        ui = gout_attr("tui", "Tui")(project, screen)
        ui.handle(" ")  # empty line: play, live since nothing is rendered
        self.assertIsNotNone(ui.player)
        self.assertTrue(ui.player.live)
        self.assertFalse(project.master.exists())
        time.sleep(0.6)
        ui.draw()
        header = next(screen.row(y) for y in range(30) if "timeline" in screen.row(y))
        self.assertIn("▶", header)
        ui.handle(" ")  # stop, keeping the playhead
        self.assertIsNone(ui.player)
        self.assertGreater(ui.playhead_ms, 400)
        stopped = ui.playhead_ms
        ui.handle(" ")
        self.assertGreaterEqual(ui.play_position_ms(), stopped)  # carries on from there
        ui.input = "stop"
        ui.submit()
        ui.input = "play 3s"
        ui.submit()
        self.assertGreaterEqual(ui.play_position_ms(), 3000)
        import curses
        ui.handle(curses.KEY_LEFT)  # empty line while playing: back 5 s (not before 0)
        self.assertLess(ui.play_position_ms(), 1000)
        ui.input = "play 3.8s"
        ui.submit()
        time.sleep(0.6)
        ui.check_player()  # it played to the end
        self.assertIsNone(ui.player)
        self.assertEqual(ui.playhead_ms, 0)
        ui.handle("x")
        ui.handle(" ")  # a space on a line with text is just a space
        self.assertEqual(ui.input, "x ")
        self.assertIsNone(ui.player)
        ui.input = ""
        ui.handle(" ")
        ui.input = "quit"
        ui.submit()  # quitting stops playback
        self.assertIsNone(ui.player)
        self.assertFalse(ui.running)
